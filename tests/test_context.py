import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vibesys.context import (
    _RunContext,
    create_candidate_context,
    create_run_context,
    setup_exp_dir,
)
from vibesys.domains.base import DomainName
from vibesys.domains.environment import EnvironmentPatch, NoopEnvironmentHooks
from vibesys.errors import ConfigurationError
from vibesys.profilers import ACTIVE_PROFILER_KINDS, ProfilerKind, ProfilerPreflightResult
from vibesys.run import GitTracker, RunLogger, RunPaths, Workspace
from vibesys.sandbox.run_environment import RunEnvironmentSpec


def _minimal_copy_context(workspace):
    ctx = object.__new__(_RunContext)
    ctx._paths = RunPaths(
        exp_dir=workspace.parent,
        log_dir=workspace.parent / "logs",
        workspace=workspace,
        run_log_path=workspace.parent / "run.log",
    )
    ctx.git_tracking = True
    ctx.EXCLUDED_WORKSPACE_DIRS = {".git", "target"}
    ctx.run_environment = SimpleNamespace(isolated=False)
    ctx.backend_impl = MagicMock()
    ctx.lprint = MagicMock()
    ctx.git = GitTracker(workspace, log=ctx.lprint, excluded_dirs=ctx.EXCLUDED_WORKSPACE_DIRS)
    ctx.implementer_backend = SimpleNamespace()
    ctx._experiment_repository = None
    return ctx


def test_setup_exp_dir_uses_unique_names_for_concurrent_default_runs(tmp_path):
    first = setup_exp_dir("test", project_root=tmp_path)
    second = setup_exp_dir("test", project_root=tmp_path)

    assert first != second
    assert first.name.endswith("-test")
    assert second.name.endswith("-test")
    assert (first / ".git").is_dir()
    assert (second / ".git").is_dir()


def test_log_switch_retargets_stderr_tee_and_restores_on_close(tmp_path):
    ctx = object.__new__(_RunContext)
    original_stderr = sys.stderr
    ctx.logger = RunLogger(tmp_path)
    ctx._paths = RunPaths(
        exp_dir=tmp_path,
        log_dir=tmp_path,
        workspace=tmp_path / "workspace",
        run_log_path=ctx.logger.path,
    )
    original_log = ctx.run_log_file

    ctx.switch_log_file("round001")

    # The unconditional tee mirrors stderr into the *current* log file,
    # stripped of ANSI escapes, while writes still reach the real stderr.
    print("\033[31mcolored diagnostic\033[0m", file=sys.stderr)
    assert ctx.run_log_path.name.endswith("-round001.log")

    ctx.logger.close()
    assert sys.stderr is original_stderr
    assert "colored diagnostic" in ctx.run_log_path.read_text()
    assert "\033[31m" not in ctx.run_log_path.read_text()
    original_log.close()


def test_input_copy_respects_source_gitignore(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text("/candidate.so\n/build/\n")
    (source / "main.rs").write_text("fn main() {}\n")
    (source / "candidate.so").write_bytes(b"stale")
    (source / "build").mkdir()
    (source / "build" / "cache").write_text("stale")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)

    destination = tmp_path / "workspace"
    workspace = Workspace(
        destination,
        run_environment=SimpleNamespace(isolated=False),
        backend=MagicMock(),
        log=MagicMock(),
        project_root=tmp_path,
        excluded_dirs={".git", "target"},
    )
    workspace.copy_dir(source, destination, respect_source_gitignore=True)

    assert (destination / "main.rs").is_file()
    assert not (destination / "candidate.so").exists()
    assert not (destination / "build").exists()


def test_trusted_input_changes_compare_against_initial_commit(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "accuracy_checker").mkdir(parents=True)
    (workspace / "accuracy_checker" / "checker.py").write_text("print('ok')\n")
    (workspace / "main.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=workspace,
        check=True,
    )

    ctx = _minimal_copy_context(workspace)
    (workspace / "main.py").write_text("VALUE = 2\n")
    assert ctx.trusted_input_changes() == []

    (workspace / "accuracy_checker" / "checker.py").write_text("print('forged')\n")
    assert ctx.trusted_input_changes() == ["accuracy_checker/checker.py"]


def test_workspace_snapshot_pushes_remote_experiment_checkpoint(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("VALUE = 1\n")
    ctx = _minimal_copy_context(workspace)
    ctx.git.init(existing=False)
    ctx._experiment_repository = MagicMock()

    (workspace / "main.py").write_text("VALUE = 2\n")
    ctx.snapshot_workspace("round-1-implementer")

    ctx._experiment_repository.sync.assert_called_once_with()


def test_directory_snapshot_pushes_remote_experiment_checkpoint(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("VALUE = 1\n")
    ctx = _minimal_copy_context(workspace)
    ctx.git_tracking = False
    ctx.workspace_files = MagicMock()
    ctx._experiment_repository = MagicMock()

    ctx.snapshot_workspace("round-1-implementer")

    snapshot = ctx.log_dir / "snapshots" / "round-1-implementer"
    assert (snapshot / "main.py").read_text() == "VALUE = 1\n"
    ctx.workspace_files.replace_external_symlinks.assert_called_once_with(snapshot)
    ctx._experiment_repository.sync.assert_called_once_with()


def test_workspace_snapshot_retries_remote_failure_without_stopping_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("VALUE = 1\n")
    ctx = _minimal_copy_context(workspace)
    ctx.git.init(existing=False)
    ctx._experiment_repository = MagicMock()
    ctx._experiment_repository.sync.side_effect = RuntimeError("network unavailable")

    ctx.snapshot_workspace("round-1-implementer")

    ctx._experiment_repository.sync.assert_called_once_with()
    ctx.lprint.assert_called_with(
        "[warn] experiment repository checkpoint push failed: network unavailable"
    )


class _FakeBackend:
    image = "fake-image"
    selected_device = None

    def __init__(self, profiler_kind=None) -> None:
        self.sandbox = MagicMock()
        if profiler_kind is not None:
            self.profiler_kind = profiler_kind

    def make_sandbox(self, *_args, **_kwargs):
        return self.sandbox

    def make_monitor(self, _log_dir):
        return None


@pytest.fixture(autouse=True)
def _native_profiler_preflight_ok(monkeypatch):
    monkeypatch.setattr(
        "vibesys.context.preflight_profiler_kind",
        lambda kind: ProfilerPreflightResult(kind, True),
    )


def _write_ref(tmp_path):
    ref_dir = tmp_path / "input"
    ref_dir.mkdir()
    ref = ref_dir / "reference.py"
    ref.write_text("pass\n")
    return ref


def _write_support_dirs(project_root):
    dirs = {
        ProfilerKind.NSYS: "nsys_profiler",
        ProfilerKind.TORCH: "torch_profiler",
        ProfilerKind.NEURON: "neuron_profiler",
        ProfilerKind.MACOS_CPU: "macos_cpu_profiler",
        ProfilerKind.LINUX_CPU: "linux_cpu_profiler",
    }
    for kind in dirs:
        source_dir = project_root / "resources" / "profilers" / kind.value
        source_dir.mkdir(parents=True)
        (source_dir / "server.py").write_text("pass\n")
    return {kind: str(project_root / "resources" / "profilers" / kind.value) for kind in dirs}


@pytest.mark.parametrize(
    ("profiler_kind", "workspace_name"),
    [
        (ProfilerKind.TORCH, "torch_profiler"),
        (ProfilerKind.NEURON, "neuron_profiler"),
    ],
)
def test_run_context_defaults_profiler_support_paths(tmp_path, profiler_kind, workspace_name):
    project_root = tmp_path / "project"
    source_dir = project_root / "resources" / "profilers" / profiler_kind.value
    source_dir.mkdir(parents=True)
    (source_dir / "server.py").write_text("pass\n")

    ref = _write_ref(tmp_path)

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=f"{profiler_kind}-defaults",
            input_path=str(ref.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            profiler_kind=profiler_kind,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
        ) as ctx,
    ):
        assert ctx.profiler_support_path == str(source_dir)
        assert (ctx.workspace / workspace_name / "server.py").is_file()


@pytest.mark.parametrize(
    "selected",
    [ProfilerKind.NONE, *sorted(ACTIVE_PROFILER_KINDS, key=lambda kind: kind.value)],
)
def test_run_context_copies_only_selected_profiler_support(tmp_path, selected):
    project_root = tmp_path / "project"
    _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    domain = (
        DomainName.GENERIC
        if selected in {ProfilerKind.MACOS_CPU, ProfilerKind.LINUX_CPU}
        else DomainName.LLM_SERVING
    )
    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=f"{selected.value}-support",
            input_path=str(ref.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            profiler_kind=selected,
            profiler_domain=domain,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
        ) as ctx,
    ):
        expected = {
            ProfilerKind.NSYS: selected is ProfilerKind.NSYS,
            ProfilerKind.TORCH: selected is ProfilerKind.TORCH,
            ProfilerKind.NEURON: selected is ProfilerKind.NEURON,
            ProfilerKind.MACOS_CPU: selected is ProfilerKind.MACOS_CPU,
            ProfilerKind.LINUX_CPU: selected is ProfilerKind.LINUX_CPU,
        }
        assert ctx.profiler_kind is selected
        assert (ctx.workspace / "nsys_profiler").exists() is expected[ProfilerKind.NSYS]
        assert (ctx.workspace / "torch_profiler").exists() is expected[ProfilerKind.TORCH]
        assert (ctx.workspace / "neuron_profiler").exists() is expected[ProfilerKind.NEURON]
        assert (ctx.workspace / "macos_cpu_profiler").exists() is expected[ProfilerKind.MACOS_CPU]
        assert (ctx.workspace / "linux_cpu_profiler").exists() is expected[ProfilerKind.LINUX_CPU]


def test_run_context_generic_auto_resolves_to_macos_profiler(tmp_path):
    project_root = tmp_path / "project"
    support_paths = _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        patch("vibesys.profilers.platform.system", return_value="Darwin"),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="generic-auto-none",
            input_path=str(ref.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            profiler_kind=ProfilerKind.AUTO,
            profiler_domain=DomainName.GENERIC,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        ) as ctx,
    ):
        assert ctx.profiler_kind is ProfilerKind.MACOS_CPU
        assert ctx.profiler_support_path == support_paths[ProfilerKind.MACOS_CPU]
        assert not (ctx.workspace / "nsys_profiler").exists()
        assert not (ctx.workspace / "torch_profiler").exists()
        assert not (ctx.workspace / "neuron_profiler").exists()
        assert (ctx.workspace / "macos_cpu_profiler").exists()
        assert not (ctx.workspace / "linux_cpu_profiler").exists()


def test_run_context_generic_auto_resolves_to_linux_profiler(tmp_path):
    project_root = tmp_path / "project"
    support_paths = _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        patch("vibesys.profilers.platform.system", return_value="Linux"),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="generic-auto-linux",
            input_path=str(ref.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            profiler_kind=ProfilerKind.AUTO,
            profiler_domain=DomainName.GENERIC,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        ) as ctx,
    ):
        assert ctx.profiler_kind is ProfilerKind.LINUX_CPU
        assert ctx.profiler_support_path == support_paths[ProfilerKind.LINUX_CPU]
        assert not (ctx.workspace / "nsys_profiler").exists()
        assert not (ctx.workspace / "torch_profiler").exists()
        assert not (ctx.workspace / "neuron_profiler").exists()
        assert not (ctx.workspace / "macos_cpu_profiler").exists()
        assert (ctx.workspace / "linux_cpu_profiler").exists()


def test_run_context_fails_fast_when_resolved_profiler_is_unusable(tmp_path):
    project_root = tmp_path / "project"
    _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    def fail_preflight(kind):
        return ProfilerPreflightResult(
            kind,
            False,
            ("perf_unavailable", "perf_event_paranoid_restrictive"),
            ("perf_path=missing", "perf_event_paranoid=3"),
        )

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        patch("vibesys.profilers.platform.system", return_value="Linux"),
        patch("vibesys.context.preflight_profiler_kind", side_effect=fail_preflight),
        pytest.raises(ConfigurationError, match="Resolved profiler 'linux_cpu' is not usable"),
    ):
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="generic-auto-linux-unusable",
            input_path=str(ref.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            profiler_kind=ProfilerKind.AUTO,
            profiler_domain=DomainName.GENERIC,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        )


@pytest.mark.parametrize(
    "profiler_kind",
    sorted(
        ACTIVE_PROFILER_KINDS - {ProfilerKind.MACOS_CPU, ProfilerKind.LINUX_CPU},
        key=lambda kind: kind.value,
    ),
)
def test_run_context_rejects_generic_explicit_active_profilers(tmp_path, profiler_kind):
    ref = _write_ref(tmp_path)

    with (
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(profiler_kind)),
        pytest.raises(ValueError, match="not supported for domain 'generic'"),
    ):
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=f"generic-{profiler_kind.value}",
            input_path=str(ref.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            profiler_kind=profiler_kind,
            profiler_domain=DomainName.GENERIC,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        )


def test_run_context_llm_auto_uses_backend_profiler_and_defaults_support_dir(tmp_path):
    project_root = tmp_path / "project"
    support_paths = _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="llm-auto-nsys",
            input_path=str(ref.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            profiler_kind=ProfilerKind.AUTO,
            profiler_domain=DomainName.LLM_SERVING,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
        ) as ctx,
    ):
        assert ctx.profiler_kind is ProfilerKind.NSYS
        assert ctx.profiler_support_path == support_paths[ProfilerKind.NSYS]
        assert (ctx.workspace / "nsys_profiler" / "server.py").is_file()
        assert not (ctx.workspace / "torch_profiler").exists()
        assert not (ctx.workspace / "neuron_profiler").exists()


def test_run_context_noop_environment_hooks_do_not_require_model_artifacts(tmp_path):
    project_root = tmp_path / "project"
    ref_dir = tmp_path / "queue" / "reference"
    ref_dir.mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="generic-reference-dir",
            input_path=str(ref_dir.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        ) as ctx,
    ):
        assert (ctx.workspace / "reference" / "reference.py").is_file()
        assert not (ref_dir / "model").exists()


def test_candidate_context_cleans_up_when_agent_runner_construction_fails(tmp_path):
    workspace = tmp_path / "candidates" / f"{tmp_path.name}-g1c2" / "workspace"
    parent = SimpleNamespace(
        exp_dir=tmp_path,
        git=MagicMock(),
        run_environment=MagicMock(),
        backend_impl=_FakeBackend(),
        EXCLUDED_WORKSPACE_DIRS={".git", "target"},
        accuracy_command="check-accuracy",
        benchmark_command="run-benchmark",
        profiler_support_path=None,
        profiler_support_name=None,
        environment_patch=SimpleNamespace(bind_mounts=()),
        skill_source_paths=[],
        model="mock-model",
        model_name="claude-sonnet-4-6",
    )
    parent.git.add_worktree.side_effect = lambda path, _commit: path.mkdir(parents=True)
    session = MagicMock()
    session.__enter__.return_value = session
    session.view = SimpleNamespace(
        paths=SimpleNamespace(
            accuracy_command="check-accuracy",
            benchmark_command="run-benchmark",
            profiler_support=None,
        ),
        cli_sandboxed=False,
        cli_modal_sandboxed=False,
    )
    session.sandbox = MagicMock()
    parent.run_environment.open.return_value = session

    with (
        patch("vibesys.context.build_agent_runner", side_effect=SystemExit("boom")),
        pytest.raises(SystemExit, match="boom"),
    ):
        create_candidate_context(
            parent,
            config={"model": {"name": "claude-sonnet-4-6"}},
            generation=1,
            child_idx=2,
            parent_commit="deadbeef",
        )

    session.__exit__.assert_called_once()
    parent.git.remove_worktree.assert_called_once_with(workspace)


def test_run_context_cleans_up_when_agent_runner_construction_fails(tmp_path):
    project_root = tmp_path / "project"
    ref = _write_ref(tmp_path)
    hooks = MagicMock()
    hooks.prepare.return_value = EnvironmentPatch()
    original_stderr = sys.stderr

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", side_effect=RuntimeError("boom")),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        pytest.raises(RuntimeError, match="boom"),
    ):
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="failed-construction",
            input_path=str(ref.parent),
            accuracy_command="check-accuracy",
            benchmark_command="run-benchmark",
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=hooks,
        )

    assert sys.stderr is original_stderr
    hooks.teardown.assert_called_once()


def test_run_context_materializes_input_project_path_dependencies(tmp_path):
    project_root = tmp_path / "project"
    input_core = project_root / "examples" / "libs" / "queue-input-core"
    input_core.mkdir(parents=True)
    (input_core / "pyproject.toml").write_text(
        "[project]\nname = 'queue-input-core'\nversion = '0.1.0'\n"
    )
    (input_core / "core.py").write_text("VALUE = 1\n")

    input_dir = project_root / "examples" / "data-structures" / "queue-spsc"
    ref_dir = input_dir / "reference"
    acc_dir = input_dir / "accuracy_checker"
    bench_dir = input_dir / "benchmark"
    ref_dir.mkdir(parents=True)
    acc_dir.mkdir()
    bench_dir.mkdir()
    (ref_dir / "reference.py").write_text("pass\n")
    (acc_dir / "checker.py").write_text("pass\n")
    (bench_dir / "benchmark.py").write_text("pass\n")
    (input_dir / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'queue-spsc-input'\n"
        "version = '0.1.0'\n"
        "dependencies = ['queue-input-core']\n"
        "\n"
        "[tool.uv.sources]\n"
        "queue-input-core = { path = '../../libs/queue-input-core', editable = true }\n"
    )

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="input-local-package",
            input_path=str(ref_dir.parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        ) as ctx,
    ):
        assert (ctx.workspace / "reference" / "reference.py").is_file()
        assert (ctx.workspace / "accuracy_checker" / "checker.py").is_file()
        assert (ctx.workspace / "benchmark" / "benchmark.py").is_file()
        assert (ctx.workspace / "_input_libs" / "queue-input-core" / "core.py").is_file()
        assert (
            "queue-input-core = { path = '_input_libs/queue-input-core', editable = true }\n"
            in (ctx.workspace / "pyproject.toml").read_text()
        )
