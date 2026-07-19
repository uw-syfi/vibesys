import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vibesys.context import _RunContext, setup_exp_dir
from vibesys.domains.base import DomainName
from vibesys.domains.environment import NoopEnvironmentHooks
from vibesys.profilers import ACTIVE_PROFILER_KINDS, ProfilerKind
from vibesys.sandbox.run_environment import RunEnvironmentSpec


def _minimal_copy_context(workspace):
    ctx = object.__new__(_RunContext)
    ctx.workspace = workspace
    ctx.git_tracking = True
    ctx.EXCLUDED_WORKSPACE_DIRS = {".git", "target"}
    ctx.run_environment = SimpleNamespace(isolated=False)
    ctx.backend_impl = MagicMock()
    ctx.lprint = MagicMock()
    return ctx


def test_setup_exp_dir_uses_unique_names_for_concurrent_default_runs(tmp_path):
    first = setup_exp_dir("test", project_root=tmp_path)
    second = setup_exp_dir("test", project_root=tmp_path)

    assert first != second
    assert first.name.endswith("-test")
    assert second.name.endswith("-test")
    assert (first / ".git").is_dir()
    assert (second / ".git").is_dir()


def test_interactive_log_switch_preserves_supervision_stderr(tmp_path):
    ctx = object.__new__(_RunContext)
    ctx.log_dir = tmp_path
    ctx.run_log_file = (tmp_path / "run.log").open("a", encoding="utf-8")
    ctx.run_log_path = tmp_path / "run.log"
    ctx._stderr_redirected = False
    ctx._original_stderr = sys.stderr
    original_log = ctx.run_log_file

    ctx.switch_log_file("round001")

    assert sys.stderr is ctx._original_stderr
    original_log.close()
    ctx.run_log_file.close()


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
    ctx = _minimal_copy_context(destination)
    ctx._copy_excluding_extras(source, destination, respect_source_gitignore=True)

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
        patch("vibesys.context._build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        _RunContext(
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

    domain = DomainName.GENERIC if selected is ProfilerKind.MACOS_CPU else DomainName.LLM_SERVING
    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context._build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        _RunContext(
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
        }
        assert ctx.profiler_kind is selected
        assert (ctx.workspace / "nsys_profiler").exists() is expected[ProfilerKind.NSYS]
        assert (ctx.workspace / "torch_profiler").exists() is expected[ProfilerKind.TORCH]
        assert (ctx.workspace / "neuron_profiler").exists() is expected[ProfilerKind.NEURON]
        assert (ctx.workspace / "macos_cpu_profiler").exists() is expected[ProfilerKind.MACOS_CPU]


def test_run_context_generic_auto_resolves_to_macos_profiler(tmp_path):
    project_root = tmp_path / "project"
    support_paths = _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    with (
        patch("vibesys.context.PROJECT_ROOT", project_root),
        patch("vibesys.context._build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        patch("vibesys.profilers.platform.system", return_value="Darwin"),
        _RunContext(
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


@pytest.mark.parametrize(
    "profiler_kind",
    sorted(ACTIVE_PROFILER_KINDS - {ProfilerKind.MACOS_CPU}, key=lambda kind: kind.value),
)
def test_run_context_rejects_generic_explicit_active_profilers(tmp_path, profiler_kind):
    ref = _write_ref(tmp_path)

    with (
        patch("vibesys.context._build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(profiler_kind)),
        pytest.raises(ValueError, match="not supported for domain 'generic'"),
    ):
        _RunContext(
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
        patch("vibesys.context._build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        _RunContext(
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
        patch("vibesys.context._build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        _RunContext(
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
        patch("vibesys.context._build_model", return_value="mock-model"),
        patch("vibesys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibesys.context.backends.get", return_value=_FakeBackend()),
        _RunContext(
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
