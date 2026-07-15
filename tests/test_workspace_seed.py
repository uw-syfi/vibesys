"""Tests for manifest-declared workspace seeds and evaluator sources."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibe_sys.cli import main
from vibe_sys.constants import DEFAULT_COMPUTE_BACKEND
from vibe_sys.context import _RunContext
from vibe_sys.domains.environment import NoopEnvironmentHooks
from vibe_sys.input_manifest import load_input_bundle
from vibe_sys.profilers import ProfilerKind
from vibe_sys.sandbox.run_environment import RunEnvironmentSpec


class _FakeBackend:
    image = "fake-image"
    selected_device = None

    def __init__(self) -> None:
        self.sandbox = MagicMock()

    def make_sandbox(self, *_args, **_kwargs):
        return self.sandbox

    def make_monitor(self, _log_dir):
        return None


def _write_bundle(project_root: Path, manifest_blocks: str = "") -> Path:
    bundle = project_root / "examples" / "data-structures" / "queue-spsc"
    bundle.mkdir(parents=True)
    (bundle / "OBJECTIVE.md").write_text("Build a queue.\n")
    (bundle / "vibesys.input.toml").write_text(
        f"""
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["accuracy-checker"]

[benchmark]
command = ["benchmark"]

{manifest_blocks}
""".lstrip()
    )
    return bundle


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


@contextmanager
def _patched_context_dependencies(project_root: Path):
    with (
        patch("vibe_sys.context.PROJECT_ROOT", project_root),
        patch("vibe_sys.context._build_model", return_value="mock-model"),
        patch("vibe_sys.context.build_agent_runner", return_value=MagicMock()),
        patch("vibe_sys.context.backends.get", return_value=_FakeBackend()),
    ):
        yield


def _make_context(input_dir: Path, seed: Path | None = None, **kwargs) -> _RunContext:
    return _RunContext(
        config={"model": {"name": "claude-sonnet-4-6"}},
        exp_name=kwargs.pop("exp_name", "workspace-seed"),
        input_path=str(input_dir),
        accuracy_command="accuracy-checker",
        benchmark_command="benchmark",
        workspace_seed=seed,
        profiler_kind=ProfilerKind.NONE,
        skills_dirs=[],
        run_environment=RunEnvironmentSpec("local"),
        environment_hooks=NoopEnvironmentHooks(),
        **kwargs,
    )


def test_all_repo_example_input_bundles_are_valid():
    project_root = Path(__file__).parents[1]
    manifests = sorted((project_root / "examples").glob("**/vibesys.input.toml"))

    assert manifests
    for manifest in manifests:
        bundle = load_input_bundle(manifest.parent, project_root=project_root)
        assert bundle.domain is bundle.manifest.agent.domain


def test_manifest_without_workspace_seed_remains_valid(tmp_path):
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root)

    loaded = load_input_bundle(bundle, project_root=project_root)

    assert loaded.workspace_seed_path is None
    assert loaded.evaluator_path is None


def test_manifest_resolves_seed_relative_to_bundle(tmp_path):
    project_root = tmp_path / "project"
    seed = project_root / "examples" / "starters" / "queue-rs"
    seed.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[workspace]\nseed = "../../starters/queue-rs"',
    )

    loaded = load_input_bundle(bundle, project_root=project_root)

    assert loaded.workspace_seed_path == seed.resolve()


def test_manifest_resolves_evaluator_relative_to_bundle(tmp_path):
    project_root = tmp_path / "project"
    evaluator = project_root / "examples" / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[evaluator]\nsource = "../../evaluators/queue"',
    )

    loaded = load_input_bundle(bundle, project_root=project_root)

    assert loaded.evaluator_path == evaluator.resolve()


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


@pytest.mark.parametrize(
    ("source_value", "error"),
    [
        ("/tmp/evaluator", "source must be relative"),
        ("../../../outside", "must resolve inside"),
        ("../../evaluators/missing", "path does not exist"),
    ],
)
def test_manifest_rejects_invalid_evaluator_paths(tmp_path, source_value, error):
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        f'[evaluator]\nsource = "{source_value}"',
    )

    with pytest.raises((FileNotFoundError, ValueError), match=error):
        load_input_bundle(bundle, project_root=project_root)


def test_manifest_rejects_evaluator_file(tmp_path):
    project_root = tmp_path / "project"
    evaluator = project_root / "examples" / "evaluators" / "not-a-directory"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("not a directory\n")
    bundle = _write_bundle(
        project_root,
        '[evaluator]\nsource = "../../evaluators/not-a-directory"',
    )

    with pytest.raises(ValueError, match="path is not a directory"):
        load_input_bundle(bundle, project_root=project_root)


def test_manifest_rejects_evaluator_symlink_that_escapes_evaluators(tmp_path):
    project_root = tmp_path / "project"
    outside = project_root / "outside"
    outside.mkdir(parents=True)
    evaluator = project_root / "examples" / "evaluators" / "escape"
    evaluator.parent.mkdir(parents=True)
    evaluator.symlink_to(outside, target_is_directory=True)
    bundle = _write_bundle(
        project_root,
        '[evaluator]\nsource = "../../evaluators/escape"',
    )

    with pytest.raises(ValueError, match="must resolve inside"):
        load_input_bundle(bundle, project_root=project_root)


def test_manifest_rejects_unknown_evaluator_keys(tmp_path):
    project_root = tmp_path / "project"
    evaluator = project_root / "examples" / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[evaluator]\nsource = "../../evaluators/queue"\nmutable = false',
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

    evaluator = project_root / "examples" / "evaluators" / "queue"
    (evaluator / "target").mkdir(parents=True)
    (evaluator / ".gitignore").write_text("target/\n")
    (evaluator / "checker.go").write_text("package main\n")
    (evaluator / "target" / "checker").write_bytes(b"build artifact")

    input_dir = project_root / "examples" / "data-structures" / "queue"
    input_dir.mkdir(parents=True)
    (input_dir / "OBJECTIVE.md").write_text("Build a queue.\n")
    (input_dir / "checker.py").write_text("pass\n")

    with _patched_context_dependencies(project_root):
        with _make_context(
            input_dir,
            seed,
            evaluator_path=evaluator,
            git_tracking=True,
        ) as ctx:
            assert ctx.workspace_seed_path == seed.resolve()
            assert ctx.evaluator_path == evaluator.resolve()
            assert (ctx.workspace / "Cargo.toml").is_file()
            assert (ctx.workspace / "src" / "lib.rs").is_file()
            assert (ctx.workspace / "OBJECTIVE.md").is_file()
            assert (ctx.workspace / "checker.py").is_file()
            assert not (ctx.workspace / "target").exists()
            assert not (ctx.workspace / "candidate.so").exists()
            assert (ctx.workspace / "_evaluator" / "queue" / "checker.go").is_file()
            assert not (ctx.workspace / "_evaluator" / "queue" / "target").exists()
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
            assert "_evaluator/queue/checker.go" in tracked

            (ctx.workspace / "_evaluator" / "queue" / "checker.go").write_text(
                "package compromised\n"
            )
            assert ctx.trusted_input_changes() == ["_evaluator/queue/checker.go"]


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

    evaluator = project_root / "examples" / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    evaluator_file = evaluator / "checker.go"
    evaluator_file.write_text("evaluator version 1\n")

    input_dir = project_root / "examples" / "data-structures" / "queue"
    input_dir.mkdir(parents=True)
    (input_dir / "OBJECTIVE.md").write_text("Build a queue.\n")

    with _patched_context_dependencies(project_root):
        with _make_context(
            input_dir,
            seed,
            evaluator_path=evaluator,
            git_tracking=True,
        ) as first:
            run_name = first.exp_dir.name
            workspace_file = first.workspace / "candidate.rs"
            workspace_file.write_text("agent version\n")
            workspace_evaluator = first.workspace / "_evaluator" / "queue" / "checker.go"
            workspace_evaluator.write_text("run evaluator\n")

        seed_file.write_text("seed version 2\n")
        evaluator_file.write_text("evaluator version 2\n")

        with _make_context(
            input_dir,
            seed,
            evaluator_path=evaluator,
            exp_name=run_name,
            existing=True,
            git_tracking=True,
        ) as resumed:
            assert (resumed.workspace / "candidate.rs").read_text() == "agent version\n"
            assert (
                resumed.workspace / "_evaluator" / "queue" / "checker.go"
            ).read_text() == "run evaluator\n"


@pytest.mark.parametrize(
    ("outer_loop", "runner_path"),
    [
        ("agent", "vibe_sys.loops.agent.loop.run_agent_loop"),
        ("evolve", "vibe_sys.loops.evolve.loop.run_evolve_loop"),
        ("openevolve", "vibe_sys.loops.openevolve.loop.run_openevolve_loop"),
        ("plain", "vibe_sys.loops.plain.loop.run_plain_loop"),
    ],
)
def test_cli_forwards_workspace_sources_to_every_outer_loop(tmp_path, outer_loop, runner_path):
    project_root = tmp_path / "project"
    seed = project_root / "examples" / "starters" / "queue"
    seed.mkdir(parents=True)
    evaluator = project_root / "examples" / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[workspace]\nseed = "../../starters/queue"\n\n'
        '[evaluator]\nsource = "../../evaluators/queue"',
    )
    argv = ["vibe-sys", "--outer-loop", outer_loop, "--input", str(bundle)]

    with (
        patch("sys.argv", argv),
        patch("vibe_sys.input_manifest.PROJECT_ROOT", project_root),
        patch(
            "vibe_sys.cli.load_config_and_skills",
            return_value=(
                {"model": {"name": "claude-sonnet-4-6"}},
                None,
                DEFAULT_COMPUTE_BACKEND,
            ),
        ),
        patch(runner_path, return_value=True) as runner,
    ):
        main()

    assert runner.call_args.kwargs["workspace_seed"] == seed.resolve()
    assert runner.call_args.kwargs["evaluator_path"] == evaluator.resolve()
