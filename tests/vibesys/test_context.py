"""Product run-resource composition through its internal module API."""

import shutil
import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Literal, TypedDict, Unpack
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict
from tests.support import run_test_command

from vibesys.api import open_run_store
from vibesys.composition import resolve_agent_specs
from vibesys.config import BUNDLED_RESOURCES, Config
from vibesys.errors import ConfigurationError
from vibesys.events import CoreEventType
from vibesys.inputs import (
    WorkspaceInput,
    WorkspaceSource,
    load_input_bundle,
    load_project_task,
)
from vibesys.orchestration.agent_options import (
    AgentOrchestrationOptions,
)
from vibesys.orchestration.profilers import (
    PROFILERS_COMMON_STAGED_NAME,
    ProfilerKind,
    profiler_definition,
    profiler_support_extra,
)
from vibesys.plugin_builtins import built_in_orchestrations
from vibesys.run import LocalRunIntegration
from vibesys.run.contracts import ResumeRef, RunRequest
from vibesys.run.resources import (
    _PreparedRun,
    open_run_resources,
)
from vs_agent.api import cli_skill_dirs
from vs_project.api import OrchestrationDescriptor, OrchestrationRunManifest, Project
from vs_runtime.api import AgentRole, boot_trace
from vs_runtime.api.infrastructure import (
    EvaluatorPackageRequirement,
    RunEnvironmentSpec,
    resolve_evaluator_package,
)
from vs_sandbox.api import HostResourceAccess
from vs_sandbox.api.evaluator_tools import tool_install_root
from vs_sandbox.api.testing import FakeComputeBackend


class _PortableStateProbe(BaseModel):
    """Strict value used to exercise generic portable-state replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round_idx: int


class _RecordingBackendFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> FakeComputeBackend:
        self.calls += 1
        return FakeComputeBackend()


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
    integration: LocalRunIntegration | None
    agent_roles: tuple[AgentRole, ...]
    backend_factory: _RecordingBackendFactory
    skills_dirs: list[str] | None


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
    )


def _create_context(
    project: Path,
    **options: Unpack[_CreateContextOptions],
) -> AbstractContextManager[_PreparedRun]:
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
    descriptor = OrchestrationDescriptor(
        id="multi-agent",
        config_version=1,
        options=(options.get("configuration") or _options()).model_dump(mode="json"),
    )
    request = RunRequest(
        project_root=project,
        orchestration=descriptor,
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
        skills_dirs=options.get("skills_dirs"),
    )
    registration = built_in_orchestrations().resolve(descriptor.id)
    plugin = registration.plugin
    agent_roles = options.get("agent_roles", plugin.agents)
    ownership = ExitStack()
    try:
        prepared = open_run_resources(
            request,
            options.get("integration") or LocalRunIntegration(),
            ownership=ownership,
            agent_specs=resolve_agent_specs(
                request.config,
                agent_roles,
                backend=request.agent_backend,
                provider=request.cli_provider,
            ),
            resume_policy=registration.resume_policy,
            backend_factory=options.get("backend_factory") or _RecordingBackendFactory(),
        )
    except BaseException as construction_error:
        try:
            ownership.close()
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-606130 [BLE001]; catching Exception would leak resources on cancellation or SystemExit, while ExitStack.__exit__ could replace the construction failure.
            construction_error.add_note(f"test resource cleanup also failed: {cleanup_error}")
        raise
    return _owned_prepared_run(prepared, ownership)


@contextmanager
def _owned_prepared_run(
    prepared: _PreparedRun,
    ownership: ExitStack,
) -> Iterator[_PreparedRun]:
    """Close resources opened by the direct composition helper."""
    with ownership:
        yield prepared


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
        project_resources = ctx.project_resources
        environment = ctx.environment_resources
        run_id = project_resources.state.run_id
        assert environment.request.workspace == project
        assert project_resources.project.root == project
        assert environment.request.log_dir == project_resources.project.state.log_directory(run_id)
        assert (
            not project_resources.state.local("test-agent")
            .external_directory()
            .is_relative_to(project)
        )
        objective_path = Path(environment.view.paths.objective)
        assert objective_path == (
            project_resources.project.state.portable_namespace(
                run_id, "runtime"
            ).external_directory()
            / "effective-objective.md"
        )
        assert objective_path.read_text() == "Make the queue faster.\n"
        assert objective_path.is_relative_to(environment.request.workspace)

        policy = environment.request.project_path_policy
        state_paths = project_resources.project.state.sandbox_paths()
        assert state_paths.read_only_path in policy.read_only_paths
        assert state_paths.hidden_path is None

    manifest = Project.open(project).state.load_run(run_id)
    assert manifest.branch == f"vibesys-runs/{run_id}"
    assert _git(project, "branch", "--show-current") == manifest.branch
    assert _git(project, "status", "--porcelain") == ""


def test_driver_skill_copies_are_not_candidate_changes(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    skills = tmp_path / "skills"
    (skills / "serving-notes").mkdir(parents=True)
    (skills / "serving-notes" / "SKILL.md").write_text(
        "---\nname: serving-notes\ndescription: Notes.\n---\nRead me.\n"
    )
    with _create_context(project, evaluator=evaluator, skills_dirs=[str(skills)]) as ctx:
        workspace = ctx.environment_resources.request.workspace
        # What an agent driver does before each turn: copy every skill into
        # the workspace root and into each CLI's skill-discovery directory.
        for target in (".", *cli_skill_dirs()):
            shutil.copytree(skills / "serving-notes", workspace / target / "serving-notes")
        (workspace / "fast_queue.py").write_text("FAST = True\n")

        changed = _git(workspace, "status", "--porcelain", "--untracked-files=all")

    assert changed.splitlines() == ["?? fast_queue.py"]


def test_context_places_evaluator_tools_in_operator_cache_and_imports_it_read_only(
    tmp_path: Path,
) -> None:
    project = tmp_path / "queue"
    _write_project(project)
    packages_root = BUNDLED_RESOURCES.directory("evaluators")
    assert packages_root is not None
    package = resolve_evaluator_package(
        packages_root,
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        ),
    )

    tools_root = Project.open(project).state.model_cache_directory("evaluator-tools")
    for name, spec in package.metadata.tools.items():
        tool_install_root(tools_root, name, spec).mkdir(parents=True)

    with _create_context(project, evaluator_package_root=package.root) as ctx:
        tools_root = ctx.project_resources.project.state.model_cache_directory("evaluator-tools")
        resources = {resource.path: resource.access for resource in ctx.agent_host_resources}
        expected_tool_roots = tuple(
            tool_install_root(tools_root, name, spec)
            for name, spec in package.metadata.tools.items()
        )

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

            assert len(changed) == 1
            assert changed[0].run_id == ctx.project_resources.state.run_id
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
        log_text = ctx.project_resources.logger.path.read_text()

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
        log_text = ctx.project_resources.logger.path.read_text()

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
        log_text = ctx.project_resources.logger.path.read_text()

    assert "boot span agent_preamble" not in log_text
    assert "boot span dispatch" not in log_text


def test_context_assembly_spans_stay_off_stderr_by_default(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Boot spans are forensics in the run log, not narration at the operator."""
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as ctx:
        log_text = ctx.project_resources.logger.path.read_text()
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
        assert "boot span context: " in ctx.project_resources.logger.path.read_text()
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
        assert ctx.facts.reference_location == ".vibesys/tasks/latency/reference/baseline.py"


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
        ) as ctx:
            run_id = ctx.project_resources.state.run_id
            workspace = ctx.environment_resources.request.workspace
            runtime_model = runs_dir / ".cache" / "llm-serving" / run_id / "model"
            copied_reference = workspace / ".vibesys" / "tasks" / "latency" / "reference"

            assert not (reference / "model").exists()
            assert not (copied_reference / "model").exists()
            assert runtime_model.resolve() == downloaded
            assert ctx.project_resources.git.trusted_input_changes() == []

        assert _git(workspace, "status", "--porcelain") == ""


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
        ) as ctx:
            runtime_model = (
                ctx.project_resources.project.state.model_cache_directory("llm-serving") / "model"
            )

            assert not (reference / "model").exists()
            assert runtime_model.resolve() == downloaded
            assert ctx.project_resources.git.trusted_input_changes() == []

        assert _git(project, "status", "--porcelain") == ""


def test_copied_run_provisions_self_contained_project_in_collection(tmp_path: Path) -> None:
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"

    with _create_context(source, runs_dir=runs_dir, evaluator=evaluator) as ctx:
        project = ctx.environment_resources.request.workspace
        run_id = ctx.project_resources.state.run_id
        assert project.parent == runs_dir
        assert project.name == run_id
        assert ctx.environment_resources.request.workspace == project
        assert (project / "queue.py").is_file()
        assert not (project / "checker").exists()
        assert (project / "_evaluator" / "checker" / "check.py").is_file()
        manifest_text = (project / "vibesys.input.toml").read_text()
        assert 'source = "_evaluator/checker"' in manifest_text
        assert "[workspace]" not in manifest_text
        assert ctx.environment_resources.request.log_dir == (
            ctx.project_resources.project.state.log_directory(run_id)
        )

    assert _git(project, "status", "--porcelain") == ""


def test_agent_v5_run_resumes_with_larger_round_budget(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.project_resources.state.run_id

    stored = Project.open(project).state.load_run(run_id)
    assert isinstance(stored, OrchestrationRunManifest)
    assert stored.orchestration.id == "multi-agent"
    assert stored.orchestration.options["max_rounds"] == 1
    view = open_run_store(Project.open(project)).get_run(run_id)
    assert view.loop == "multi-agent"
    registration = built_in_orchestrations().resolve(stored.orchestration.id)
    assert registration.project is not None

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


@pytest.mark.parametrize(
    ("change", "expected_detail"),
    [
        ("add", "missing recorded roles: auditor"),
        ("remove", "unknown recorded roles: profiler"),
    ],
)
def test_agent_v5_resume_rejects_changed_plugin_role_catalog(
    tmp_path: Path,
    change: Literal["add", "remove"],
    expected_detail: str,
) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.project_resources.state.run_id
    plugin_roles = built_in_orchestrations().resolve("multi-agent").plugin.agents
    selected_roles = (
        (*plugin_roles, AgentRole(id="auditor", system_prompt="audit"))
        if change == "add"
        else tuple(role for role in plugin_roles if role.id != "profiler")
    )
    backend_factory = _RecordingBackendFactory()

    with pytest.raises(ConfigurationError, match=expected_detail):
        _create_context(
            project,
            evaluator=evaluator,
            exp_name=run_id,
            existing=True,
            agent_roles=selected_roles,
            backend_factory=backend_factory,
        )
    assert backend_factory.calls == 0


def test_collection_resume_pushes_existing_origin_on_teardown(tmp_path: Path) -> None:
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"
    with _create_context(source, runs_dir=runs_dir, evaluator=evaluator) as first:
        project = first.environment_resources.request.workspace
        run_id = first.project_resources.state.run_id

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


def test_collection_resume_accepts_declared_workspace_sources(tmp_path: Path) -> None:
    # A repository task keeps [[workspace.sources]] in the copied project, and
    # the copy already holds them, so resuming it must not demand --runs-dir again.
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"
    with _create_context(source, runs_dir=runs_dir, evaluator=evaluator) as first:
        project = first.environment_resources.request.workspace
        run_id = first.project_resources.state.run_id
    library = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="library",
    )

    with _create_context(
        project,
        runs_dir=runs_dir,
        evaluator=project / "_evaluator" / "checker",
        exp_name=run_id,
        existing=True,
        workspace_sources=(library,),
    ) as resumed:
        assert resumed.project_resources.state.run_id == run_id


def test_late_construction_failure_does_not_advance_remote_run_branch(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.project_resources.state.run_id

    remote = tmp_path / "remote.git"
    run_test_command(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git(project, "remote", "add", "origin", str(remote))
    _git(project, "push", "-q", "-u", "origin", f"vibesys-runs/{run_id}")
    published = _git(remote, "rev-parse", f"refs/heads/vibesys-runs/{run_id}")
    integration = LocalRunIntegration()

    def reject_resources(_resources: object) -> None:
        message = "resource publication failed"
        raise RuntimeError(message)

    integration.add_resource_listener(reject_resources)
    try:
        with pytest.raises(RuntimeError, match="resource publication failed"):
            _create_context(
                project,
                evaluator=evaluator,
                exp_name=run_id,
                existing=True,
                configuration=_options(max_rounds=2),
                integration=integration,
            )
    finally:
        integration.close()

    assert _git(remote, "rev-parse", f"refs/heads/vibesys-runs/{run_id}") == published


def test_direct_resume_republishes_an_already_published_run(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.project_resources.state.run_id

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
        patch("vibesys.run.resources.ExperimentRepository.push") as push,
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
        run_id = first.project_resources.state.run_id

    remote = tmp_path / "remote.git"
    run_test_command(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
    )
    _git(project, "remote", "add", "origin", str(remote))

    with (
        patch("vibesys.run.resources.ExperimentRepository.push") as push,
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
    manifest = project / "vibesys.input.toml"
    manifest.write_text(
        manifest.read_text().replace('domain = "generic"', 'domain = "microservices"')
    )
    configuration = _options().model_copy(
        update={
            "agent_backend": "cli",
            "agent_driver": "omnigent",
            "cli_provider": "codex",
            "profiler": "otel",
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
        profiler_kind=ProfilerKind.OTEL,
        agent_backend=None,
    ) as context:
        assert context.facts.profiler_id == ProfilerKind.OTEL.value


def test_portable_state_snapshot_replaces_namespace_exactly(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)

    with _create_context(project, evaluator=evaluator) as ctx:
        state = ctx.project_resources.state.portable("evolve")
        state.save("old.json", _PortableStateProbe(round_idx=1))
        ctx.project_resources.state.commit("state 1", state)

        state.delete("old.json")
        state.save("new.json", _PortableStateProbe(round_idx=2))
        ctx.project_resources.state.commit("state 2", state)

    tree = _git(project, "ls-tree", "-r", "--name-only", "HEAD")
    portable = ctx.project_resources.project.state.portable_namespace(
        ctx.project_resources.state.run_id, "evolve"
    )
    assert portable.agent_visible_path("new.json") in tree
    assert portable.agent_visible_path("old.json") not in tree


def test_log_switch_retargets_stderr_tee(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    original_stderr = sys.stderr
    with _create_context(project, evaluator=evaluator) as ctx:
        original_file = ctx.project_resources.logger.file
        ctx.project_resources.logger.switch("round001")

        assert original_file.closed
        sys.stderr.write("\033[31mcolored diagnostic\033[0m\n")
        run_log_path = ctx.project_resources.logger.path

    assert sys.stderr is original_stderr
    assert "colored diagnostic" in run_log_path.read_text()
    assert "\033[31m" not in run_log_path.read_text()


def test_profiler_support_extra_includes_shared_runtime_and_declared_extras() -> None:
    """rocprof declares torch as an extra plugin dir; profilers_common is universal."""
    definition = profiler_definition(ProfilerKind.ROCPROF)

    extra = profiler_support_extra(definition)
    names = [name for _path, name in extra]

    assert names[0] == PROFILERS_COMMON_STAGED_NAME
    assert "torch_profiler" in names
    for path, _name in extra:
        assert Path(path).is_dir()


def test_profiler_support_extra_without_declared_extras_is_just_the_shared_runtime() -> None:
    definition = profiler_definition(ProfilerKind.NSYS)

    extra = profiler_support_extra(definition)

    assert [name for _path, name in extra] == [PROFILERS_COMMON_STAGED_NAME]
