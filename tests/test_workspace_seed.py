"""Tests for manifest-declared candidate workspace seeds."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.cli import main
from vibe_serve.constants import DEFAULT_COMPUTE_BACKEND
from vibe_serve.context import _RunContext
from vibe_serve.domains.environment import NoopEnvironmentHooks
from vibe_serve.input_manifest import load_input_bundle
from vibe_serve.profilers import ProfilerKind
from vibe_serve.sandbox.run_environment import RunEnvironmentSpec


class _FakeBackend:
    image = "fake-image"
    selected_device = None

    def __init__(self) -> None:
        self.sandbox = MagicMock()

    def make_sandbox(self, *_args, **_kwargs):
        return self.sandbox

    def make_monitor(self, _log_dir):
        return None


def _write_bundle(project_root: Path, workspace_block: str = "") -> Path:
    bundle = project_root / "examples" / "data-structures" / "queue-spsc"
    bundle.mkdir(parents=True)
    (bundle / "OBJECTIVE.md").write_text("Build a queue.\n")
    (bundle / "vibeserve.input.toml").write_text(
        f"""
version = 1

[accuracy]
command = ["accuracy-checker"]

[benchmark]
command = ["benchmark"]

{workspace_block}
""".lstrip()
    )
    return bundle


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


@contextmanager
def _patched_context_dependencies(project_root: Path):
    with (
        patch("vibe_serve.context.PROJECT_ROOT", project_root),
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_serve.context.backends.get", return_value=_FakeBackend()),
    ):
        yield


def _make_context(input_dir: Path, seed: Path, **kwargs) -> _RunContext:
    return _RunContext(
        config={"model": {"name": "claude-sonnet-4-6"}},
        exp_name=kwargs.pop("exp_name", "workspace-seed"),
        input_path=str(input_dir),
        accuracy_command="accuracy-checker",
        benchmark_command="benchmark",
        workspace_seed=str(seed),
        profiler_kind=ProfilerKind.NONE,
        skills_dirs=[],
        run_environment=RunEnvironmentSpec("local"),
        environment_hooks=NoopEnvironmentHooks(),
        **kwargs,
    )


def test_manifest_without_workspace_seed_remains_valid(tmp_path):
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root)

    loaded = load_input_bundle(bundle, project_root=project_root)

    assert loaded.workspace_seed_path is None


def test_manifest_resolves_seed_relative_to_bundle(tmp_path):
    project_root = tmp_path / "project"
    seed = project_root / "examples" / "starters" / "queue-copying-rust"
    seed.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[workspace]\nseed = "../../starters/queue-copying-rust"',
    )

    loaded = load_input_bundle(bundle, project_root=project_root)

    assert loaded.workspace_seed_path == seed.resolve()


@pytest.mark.parametrize(
    ("seed_value", "error"),
    [
        ("/tmp/candidate", "seed must be relative"),
        ("../../../outside", "must resolve inside"),
        ("../../starters/missing", "path does not exist"),
    ],
)
def test_manifest_rejects_invalid_seed_paths(tmp_path, seed_value, error):
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        f'[workspace]\nseed = "{seed_value}"',
    )

    with pytest.raises((FileNotFoundError, ValueError), match=error):
        load_input_bundle(bundle, project_root=project_root)


def test_manifest_rejects_seed_file(tmp_path):
    project_root = tmp_path / "project"
    seed = project_root / "examples" / "starters" / "not-a-directory"
    seed.parent.mkdir(parents=True)
    seed.write_text("not a directory\n")
    bundle = _write_bundle(
        project_root,
        '[workspace]\nseed = "../../starters/not-a-directory"',
    )

    with pytest.raises(ValueError, match="path is not a directory"):
        load_input_bundle(bundle, project_root=project_root)


def test_manifest_rejects_seed_symlink_that_escapes_starters(tmp_path):
    project_root = tmp_path / "project"
    outside = project_root / "outside"
    outside.mkdir(parents=True)
    seed = project_root / "examples" / "starters" / "escape"
    seed.parent.mkdir(parents=True)
    seed.symlink_to(outside, target_is_directory=True)
    bundle = _write_bundle(
        project_root,
        '[workspace]\nseed = "../../starters/escape"',
    )

    with pytest.raises(ValueError, match="must resolve inside"):
        load_input_bundle(bundle, project_root=project_root)


def test_manifest_rejects_unknown_workspace_keys(tmp_path):
    project_root = tmp_path / "project"
    seed = project_root / "examples" / "starters" / "queue"
    seed.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[workspace]\nseed = "../../starters/queue"\nmode = "overlay"',
    )

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_input_bundle(bundle, project_root=project_root)


def test_fresh_workspace_materializes_seed_and_preserves_it_in_initial_commit(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _init_git_repo(project_root)

    seed = project_root / "examples" / "starters" / "queue"
    (seed / "src").mkdir(parents=True)
    (seed / "target").mkdir()
    (seed / ".gitignore").write_text("target/\n*.so\n")
    (seed / "Cargo.toml").write_text("[package]\nname = 'candidate'\n")
    (seed / "src" / "lib.rs").write_text("pub fn queue() {}\n")
    (seed / "target" / "debug.o").write_bytes(b"build artifact")
    (seed / "candidate.so").write_bytes(b"shared library")

    input_dir = project_root / "examples" / "data-structures" / "queue"
    input_dir.mkdir(parents=True)
    (input_dir / "OBJECTIVE.md").write_text("Build a queue.\n")
    (input_dir / "checker.py").write_text("pass\n")

    with _patched_context_dependencies(project_root):
        with _make_context(input_dir, seed, git_tracking=True) as ctx:
            assert (ctx.workspace / "Cargo.toml").is_file()
            assert (ctx.workspace / "src" / "lib.rs").is_file()
            assert (ctx.workspace / "OBJECTIVE.md").is_file()
            assert (ctx.workspace / "checker.py").is_file()
            assert not (ctx.workspace / "target").exists()
            assert not (ctx.workspace / "candidate.so").exists()
            assert "target/\n" in (ctx.workspace / ".gitignore").read_text()

            tracked = subprocess.run(
                ["git", "ls-files"],
                cwd=ctx.workspace,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            assert "Cargo.toml" in tracked
            assert "src/lib.rs" in tracked
            assert "OBJECTIVE.md" in tracked


def test_input_collision_is_rejected_before_overlay(tmp_path):
    seed = tmp_path / "seed"
    input_dir = tmp_path / "input"
    workspace = tmp_path / "workspace"
    seed.mkdir()
    input_dir.mkdir()
    (seed / "shared").mkdir()
    (seed / "shared" / "seed.txt").write_text("seed\n")
    (input_dir / "input-only.txt").write_text("input\n")
    (input_dir / "shared").write_text("input collision\n")

    ctx = object.__new__(_RunContext)
    ctx.EXCLUDED_WORKSPACE_DIRS = set()
    ctx.run_environment = MagicMock(isolated=False)
    ctx.backend_impl = MagicMock()
    ctx.lprint = MagicMock()
    ctx._copy_excluding_extras(seed, workspace)

    with pytest.raises(ValueError, match="same paths: shared"):
        ctx._copy_excluding_extras(input_dir, workspace, reject_collisions=True)

    assert (workspace / "shared" / "seed.txt").read_text() == "seed\n"
    assert not (workspace / "input-only.txt").exists()


def test_resume_does_not_refresh_workspace_from_seed(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _init_git_repo(project_root)

    seed = project_root / "examples" / "starters" / "queue"
    seed.mkdir(parents=True)
    seed_file = seed / "candidate.rs"
    seed_file.write_text("seed version 1\n")

    input_dir = project_root / "examples" / "data-structures" / "queue"
    input_dir.mkdir(parents=True)
    (input_dir / "OBJECTIVE.md").write_text("Build a queue.\n")

    with _patched_context_dependencies(project_root):
        with _make_context(input_dir, seed, git_tracking=True) as first:
            run_name = first.exp_dir.name
            workspace_file = first.workspace / "candidate.rs"
            workspace_file.write_text("agent version\n")

        seed_file.write_text("seed version 2\n")

        with _make_context(
            input_dir,
            seed,
            exp_name=run_name,
            existing=True,
            git_tracking=True,
        ) as resumed:
            assert (resumed.workspace / "candidate.rs").read_text() == "agent version\n"


@pytest.mark.parametrize(
    ("outer_loop", "runner_path"),
    [
        ("agent", "vibe_serve.loops.agent.loop.run_agent_loop"),
        ("evolve", "vibe_serve.loops.evolve.loop.run_evolve_loop"),
        ("openevolve", "vibe_serve.loops.openevolve.loop.run_openevolve_loop"),
        ("plain", "vibe_serve.loops.plain.loop.run_plain_loop"),
    ],
)
def test_cli_forwards_workspace_seed_to_every_outer_loop(tmp_path, outer_loop, runner_path):
    project_root = tmp_path / "project"
    seed = project_root / "examples" / "starters" / "queue"
    seed.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[workspace]\nseed = "../../starters/queue"',
    )
    argv = ["vibe-serve", "--outer-loop", outer_loop, "--input", str(bundle)]

    with (
        patch("sys.argv", argv),
        patch("vibe_serve.input_manifest.PROJECT_ROOT", project_root),
        patch(
            "vibe_serve.cli.load_config_and_skills",
            return_value=(
                {"model": {"name": "claude-sonnet-4-6"}},
                None,
                DEFAULT_COMPUTE_BACKEND,
            ),
        ),
        patch(runner_path, return_value=True) as runner,
    ):
        main()

    assert runner.call_args.kwargs["workspace_seed"] == str(seed.resolve())
