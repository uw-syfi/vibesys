"""Contracts for manifest-declared workspace and evaluator sources."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vibesys.evaluators.input_manifest import (
    InputBundle,
    WorkspaceSource,
    load_input_bundle,
    load_project_task,
)
from vibesys.run import Workspace
from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path


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


def _workspace(root: Path, *, excluded_dirs: set[str] | None = None) -> Workspace:
    return Workspace(
        root,
        run_environment=MagicMock(isolated=False),
        backend=MagicMock(),
        log=MagicMock(),
        project_root=root.parent,
        excluded_dirs=excluded_dirs or set(),
    )


def test_all_repo_example_input_bundles_are_valid(
    example_input_bundles: tuple[InputBundle, ...],
) -> None:
    for bundle in example_input_bundles:
        assert bundle.domain is bundle.manifest.agent.domain


def test_manifest_without_external_sources_is_valid(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root)

    loaded = load_input_bundle(bundle)

    assert loaded.workspace_sources == ()
    assert loaded.evaluator_path is None
    assert loaded.dockerfile_path is None


def test_repository_task_exposes_its_optional_dockerfile(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    task_root = project_root / ".vibesys" / "tasks" / "queue"
    task_root.mkdir(parents=True)
    (task_root / "OBJECTIVE.md").write_text("Build a queue.\n", encoding="utf-8")
    (task_root / "vibesys.input.toml").write_text(
        """version = 1

[agent]
domain = "generic"

[accuracy]
command = ["accuracy-checker"]

[benchmark]
command = ["benchmark"]
""",
        encoding="utf-8",
    )
    dockerfile = task_root / "Dockerfile"
    dockerfile.write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    project = Project.open(project_root)

    loaded = load_project_task(project, project.select_task("queue"))

    assert loaded.dockerfile_path == dockerfile.resolve()


def test_legacy_bundle_does_not_discover_dockerfile(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root)
    (bundle / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")

    loaded = load_input_bundle(bundle)

    assert loaded.dockerfile_path is None


def test_manifest_resolves_modal_entrypoint_from_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        '[environment.modal]\nentrypoint = "deploy/service.py"',
    )
    entrypoint = bundle / "deploy" / "service.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text("app = object()\n")

    loaded = load_input_bundle(bundle)

    assert loaded.modal_entrypoint == "deploy/service.py"


@pytest.mark.parametrize(
    ("entrypoint", "error"),
    [
        ("", "non-empty path"),
        (".", "current, or parent"),
        ("../service.py", "current, or parent"),
        ("/absolute/service.py", "relative to the project root"),
        ("missing.py", "does not exist"),
    ],
)
def test_manifest_rejects_invalid_modal_entrypoint(
    tmp_path: Path,
    entrypoint: str,
    error: str,
) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        f'[environment.modal]\nentrypoint = "{entrypoint}"',
    )

    with pytest.raises((FileNotFoundError, ValueError), match=error):
        load_input_bundle(bundle)


def test_manifest_rejects_modal_entrypoint_directory_and_escape(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    directory_bundle = _write_bundle(
        project_root,
        '[environment.modal]\nentrypoint = "deploy"',
    )
    (directory_bundle / "deploy").mkdir()

    with pytest.raises(ValueError, match="is not a file"):
        load_input_bundle(directory_bundle)

    escaping_bundle = tmp_path / "escaping"
    _write_bundle(
        escaping_bundle,
        '[environment.modal]\nentrypoint = "service.py"',
    )
    outside = tmp_path / "outside.py"
    outside.write_text("app = object()\n")
    task = escaping_bundle / "examples" / "data-structures" / "queue-spsc"
    (task / "service.py").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes the project"):
        load_input_bundle(task)


def test_manifest_allows_contained_modal_entrypoint_symlink(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        '[environment.modal]\nentrypoint = "service.py"',
    )
    target = bundle / "deploy" / "service.py"
    target.parent.mkdir()
    target.write_text("app = object()\n")
    (bundle / "service.py").symlink_to(target.relative_to(bundle))

    assert load_input_bundle(bundle).modal_entrypoint == "service.py"


def test_manifest_rejects_unknown_modal_environment_keys(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        '[environment.modal]\nentrypoint = "service.py"\nregion = "us-east"',
    )
    (bundle / "service.py").write_text("app = object()\n")

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_input_bundle(bundle)


def test_manifest_resolves_workspace_sources(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        """
[workspace]

[[workspace.sources]]
name = "vllm"
repo = "https://github.com/vllm-project/vllm.git"
commit = "0123456789abcdef0123456789abcdef01234567"
dest = "vllm"
""",
    )

    loaded = load_input_bundle(bundle)

    assert loaded.workspace_sources == (
        WorkspaceSource(
            name="vllm",
            repo="https://github.com/vllm-project/vllm.git",
            commit="0123456789abcdef0123456789abcdef01234567",
            dest="vllm",
        ),
    )


@pytest.mark.parametrize(
    ("source_block", "error"),
    [
        (
            'name = "vllm"\nrepo = "https://github.com/vllm-project/vllm.git"\n'
            'commit = "main"\ndest = "vllm"',
            "commit must be a 7-64 character hexadecimal hash",
        ),
        (
            'name = "vllm"\nrepo = "https://github.com/vllm-project/vllm.git"\n'
            'commit = "0123456"\ndest = "../vllm"',
            "dest must not contain",
        ),
        (
            'name = "vllm"\nrepo = "ftp://example.invalid/vllm.git"\n'
            'commit = "0123456"\ndest = "vllm"',
            "unsupported repo URL scheme",
        ),
    ],
)
def test_manifest_rejects_invalid_workspace_sources(
    tmp_path: Path,
    source_block: str,
    error: str,
) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        f"""
[workspace]

[[workspace.sources]]
{source_block}
""",
    )

    with pytest.raises(ValueError, match=error):
        load_input_bundle(bundle)


def test_manifest_rejects_duplicate_workspace_source_destinations(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        """
[workspace]

[[workspace.sources]]
name = "first"
repo = "https://example.invalid/first.git"
commit = "0123456"
dest = "src"

[[workspace.sources]]
name = "second"
repo = "https://example.invalid/second.git"
commit = "abcdef0"
dest = "src"
""",
    )

    with pytest.raises(ValueError, match="duplicate workspace source destination"):
        load_input_bundle(bundle)


def test_manifest_resolves_evaluator_relative_to_bundle(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    evaluator = project_root / "examples" / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    bundle = _write_bundle(project_root, '[evaluator]\nsource = "../../evaluators/queue"')

    loaded = load_input_bundle(bundle)

    assert loaded.evaluator_path == evaluator.resolve()


def test_manifest_resolves_bundle_local_evaluator(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root, '[evaluator]\nsource = "_evaluator_src"')
    evaluator = bundle / "_evaluator_src"
    evaluator.mkdir()

    loaded = load_input_bundle(bundle)

    assert loaded.evaluator_path == evaluator.resolve()


def test_manifest_rejects_removed_workspace_seed(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root, '[workspace]\nseed = "_seed"')

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_input_bundle(bundle)


@pytest.mark.parametrize(
    ("source_value", "error"),
    [
        ("/absolute/evaluator", "source must be relative"),
        ("../../../outside", "path does not exist"),
        ("../../evaluators/missing", "path does not exist"),
    ],
)
def test_manifest_rejects_invalid_evaluator_paths(
    tmp_path: Path,
    source_value: str,
    error: str,
) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root, f'[evaluator]\nsource = "{source_value}"')

    with pytest.raises((FileNotFoundError, ValueError), match=error):
        load_input_bundle(bundle)


def test_manifest_rejects_evaluator_file(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    evaluator = project_root / "examples" / "evaluators" / "not-a-directory"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("not a directory\n")
    bundle = _write_bundle(
        project_root,
        '[evaluator]\nsource = "../../evaluators/not-a-directory"',
    )

    with pytest.raises(ValueError, match="path is not a directory"):
        load_input_bundle(bundle)


def test_manifest_allows_explicit_relative_evaluator_outside_example_tree(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    outside = project_root / "outside"
    outside.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[evaluator]\nsource = "../../../outside"',
    )

    assert load_input_bundle(bundle).evaluator_path == outside.resolve()


def test_manifest_rejects_unknown_evaluator_keys(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    evaluator = project_root / "examples" / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    bundle = _write_bundle(
        project_root,
        '[evaluator]\nsource = "../../evaluators/queue"\nmutable = false',
    )

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_input_bundle(bundle)


def test_manifest_accepts_benchmark_result_protocol(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle_path = _write_bundle(project_root, "result_protocol = 2")

    bundle = load_input_bundle(bundle_path)

    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_result is None


def test_manifest_rejects_both_benchmark_result_contracts(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(
        project_root,
        "result_protocol = 2\n\n[benchmark.result]\n"
        'json_argument = "--output-json"\nmetric = "ops"',
    )

    with pytest.raises(
        ValueError,
        match=re.escape("benchmark.result and benchmark.result_protocol are mutually exclusive"),
    ):
        load_input_bundle(bundle)


@pytest.mark.parametrize("version", ["1", "3"])
def test_manifest_rejects_unsupported_benchmark_result_protocol(
    tmp_path: Path, version: str
) -> None:
    """Version 1 is superseded and version 3 does not exist; neither may be declared."""
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root, f"result_protocol = {version}")

    with pytest.raises(ValueError, match="result_protocol"):
        load_input_bundle(bundle)


def test_manifest_rejects_unknown_top_level_tables(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    bundle = _write_bundle(project_root, "[unknown]\nvalue = true")

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_input_bundle(bundle)


def test_workspace_source_destination_collision_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    workspace = _workspace(root)
    workspace.create()
    (root / "library").mkdir()
    source = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="library",
    )

    with pytest.raises(ValueError, match="destination already exists"):
        workspace.materialize_git_source(source)


def test_workspace_source_destination_cannot_use_excluded_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    workspace = _workspace(root, excluded_dirs={"repos", "target"})
    workspace.create()
    source = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="repos/library",
    )

    with pytest.raises(ValueError, match="excluded path component"):
        workspace.materialize_git_source(source)

    assert not (root / "repos").exists()
