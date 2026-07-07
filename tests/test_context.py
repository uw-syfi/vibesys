from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.context import _RunContext
from vibe_serve.sandbox.run_environment import RunEnvironmentSpec


class _FakeBackend:
    image = "fake-image"
    profiler_kind = None
    selected_device = None

    def __init__(self, profiler_kind=None) -> None:
        self.profiler_kind = profiler_kind
        self.sandbox = MagicMock()

    def make_sandbox(self, *_args, **_kwargs):
        return self.sandbox

    def make_monitor(self, _log_dir):
        return None


@pytest.mark.parametrize(
    ("profiler_kind", "attr", "workspace_name"),
    [
        ("torch", "torch_profiler_path", "torch_profiler"),
        ("neuron", "neuron_profiler_path", "neuron_profiler"),
    ],
)
def test_run_context_defaults_profiler_support_paths(tmp_path, profiler_kind, attr, workspace_name):
    project_root = tmp_path / "project"
    source_dir = project_root / "examples" / "support" / workspace_name
    source_dir.mkdir(parents=True)
    (source_dir / "server.py").write_text("pass\n")

    ref_dir = tmp_path / "input"
    ref_dir.mkdir()
    ref = ref_dir / "reference.py"
    ref.write_text("pass\n")

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


def test_run_context_auto_profiler_uses_cpu_backend_profiler(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    ref_dir = tmp_path / "input"
    ref_dir.mkdir()
    ref = ref_dir / "reference.py"
    ref.write_text("pass\n")

    with (
        patch("vibe_serve.context.PROJECT_ROOT", project_root),
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend(profiler_kind="cpu")),
        _RunContext(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="cpu-auto-profiler",
            reference_path=str(ref),
            profiler_kind="auto",
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
        ) as ctx,
    ):
        assert ctx.profiler_kind == "cpu"
