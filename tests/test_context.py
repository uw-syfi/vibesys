from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.context import _RunContext
from vibe_serve.domains.base import DomainName
from vibe_serve.domains.environment import NoopEnvironmentHooks
from vibe_serve.profilers import ACTIVE_PROFILER_KINDS, ProfilerKind
from vibe_serve.sandbox.run_environment import RunEnvironmentSpec


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
    }
    for workspace_name in dirs.values():
        source_dir = project_root / "examples" / "support" / workspace_name
        source_dir.mkdir(parents=True)
        (source_dir / "server.py").write_text("pass\n")
    return {
        kind: str(project_root / "examples" / "support" / workspace_name)
        for kind, workspace_name in dirs.items()
    }


@pytest.mark.parametrize(
    ("profiler_kind", "attr", "workspace_name"),
    [
        (ProfilerKind.TORCH, "torch_profiler_path", "torch_profiler"),
        (ProfilerKind.NEURON, "neuron_profiler_path", "neuron_profiler"),
    ],
)
def test_run_context_defaults_profiler_support_paths(tmp_path, profiler_kind, attr, workspace_name):
    project_root = tmp_path / "project"
    source_dir = project_root / "examples" / "support" / workspace_name
    source_dir.mkdir(parents=True)
    (source_dir / "server.py").write_text("pass\n")

    ref = _write_ref(tmp_path)

    with (
        patch("vibe_serve.context.PROJECT_ROOT", project_root),
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend()),
        _RunContext(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=f"{profiler_kind}-defaults",
            reference_path=str(ref),
            profiler_kind=profiler_kind,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
        ) as ctx,
    ):
        assert getattr(ctx, attr) == str(source_dir)
        assert (ctx.workspace / workspace_name / "server.py").is_file()


@pytest.mark.parametrize(
    "selected",
    [ProfilerKind.NONE, *sorted(ACTIVE_PROFILER_KINDS, key=lambda kind: kind.value)],
)
def test_run_context_copies_only_selected_profiler_support(tmp_path, selected):
    project_root = tmp_path / "project"
    support_paths = _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    with (
        patch("vibe_serve.context.PROJECT_ROOT", project_root),
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend()),
        _RunContext(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=f"{selected.value}-support",
            reference_path=str(ref),
            profiler_kind=selected,
            nsys_profiler=support_paths[ProfilerKind.NSYS],
            torch_profiler=support_paths[ProfilerKind.TORCH],
            neuron_profiler=support_paths[ProfilerKind.NEURON],
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
        ) as ctx,
    ):
        expected = {
            ProfilerKind.NSYS: selected is ProfilerKind.NSYS,
            ProfilerKind.TORCH: selected is ProfilerKind.TORCH,
            ProfilerKind.NEURON: selected is ProfilerKind.NEURON,
        }
        assert ctx.profiler_kind is selected
        assert (ctx.workspace / "nsys_profiler").exists() is expected[ProfilerKind.NSYS]
        assert (ctx.workspace / "torch_profiler").exists() is expected[ProfilerKind.TORCH]
        assert (ctx.workspace / "neuron_profiler").exists() is expected[ProfilerKind.NEURON]


def test_run_context_generic_auto_resolves_to_none_without_profiler_support(tmp_path):
    project_root = tmp_path / "project"
    support_paths = _write_support_dirs(project_root)
    ref = _write_ref(tmp_path)

    with (
        patch("vibe_serve.context.PROJECT_ROOT", project_root),
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        _RunContext(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="generic-auto-none",
            reference_path=str(ref),
            profiler_kind=ProfilerKind.AUTO,
            profiler_domain=DomainName.GENERIC,
            nsys_profiler=support_paths[ProfilerKind.NSYS],
            torch_profiler=support_paths[ProfilerKind.TORCH],
            neuron_profiler=support_paths[ProfilerKind.NEURON],
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        ) as ctx,
    ):
        assert ctx.profiler_kind is ProfilerKind.NONE
        assert ctx.nsys_profiler_path is None
        assert ctx.torch_profiler_path is None
        assert ctx.neuron_profiler_path is None
        assert not (ctx.workspace / "nsys_profiler").exists()
        assert not (ctx.workspace / "torch_profiler").exists()
        assert not (ctx.workspace / "neuron_profiler").exists()


@pytest.mark.parametrize(
    "profiler_kind",
    sorted(ACTIVE_PROFILER_KINDS, key=lambda kind: kind.value),
)
def test_run_context_rejects_generic_explicit_active_profilers(tmp_path, profiler_kind):
    ref = _write_ref(tmp_path)

    with (
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend(profiler_kind)),
        pytest.raises(ValueError, match="not supported for domain 'generic'"),
    ):
        _RunContext(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=f"generic-{profiler_kind.value}",
            reference_path=str(ref),
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
        patch("vibe_serve.context.PROJECT_ROOT", project_root),
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend(ProfilerKind.NSYS)),
        _RunContext(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="llm-auto-nsys",
            reference_path=str(ref),
            profiler_kind=ProfilerKind.AUTO,
            profiler_domain=DomainName.LLM_SERVING,
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
        ) as ctx,
    ):
        assert ctx.profiler_kind is ProfilerKind.NSYS
        assert ctx.nsys_profiler_path == support_paths[ProfilerKind.NSYS]
        assert (ctx.workspace / "nsys_profiler" / "server.py").is_file()
        assert not (ctx.workspace / "torch_profiler").exists()
        assert not (ctx.workspace / "neuron_profiler").exists()


def test_run_context_noop_environment_hooks_do_not_require_model_artifacts(tmp_path):
    project_root = tmp_path / "project"
    ref_dir = tmp_path / "queue" / "reference"
    ref_dir.mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")

    with (
        patch("vibe_serve.context.PROJECT_ROOT", project_root),
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend()),
        _RunContext(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="generic-reference-dir",
            reference_path=str(ref_dir),
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=NoopEnvironmentHooks(),
        ) as ctx,
    ):
        assert (ctx.workspace / "reference" / "reference.py").is_file()
        assert not (ref_dir / "model").exists()
