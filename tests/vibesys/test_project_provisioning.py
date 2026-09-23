"""Contracts for provisioning a copied directory as a canonical project."""

from __future__ import annotations

import subprocess
import tomllib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vibesys.evaluators.input_manifest import InputManifest, WorkspaceSource, load_input_bundle
from vibesys.run.project import (
    ProjectProvisioningError,
    ProjectProvisioningSpec,
    provision_project,
)
from vibesys.run.workspace import Workspace
from vibesys.sandbox.run_environment import LocalEnvironment
from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path


def _workspace(destination: Path, *, project_root: Path) -> Workspace:
    return Workspace(
        destination,
        run_environment=LocalEnvironment(),
        backend=MagicMock(),
        log=MagicMock(),
        project_root=project_root,
    )


def _write_input(root: Path, extra_manifest: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Make the candidate faster.\n")
    (root / "candidate.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        """\
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "check.py"]
timeout_seconds = 10

[benchmark]
command = ["python", "benchmark.py"]
timeout_seconds = 20

[benchmark.result]
json_argument = "--output-json"
metric = "throughput"
"""
        + extra_manifest
    )
    return root


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_provision_project_places_source_at_root_and_removes_private_files(
    tmp_path: Path,
) -> None:
    input_root = _write_input(tmp_path / "input")
    (input_root / ".git").mkdir()
    Project.open(input_root).state.create_project("input")
    (input_root / "agent.toml").write_text("[model]\nname = 'private'\n")
    (input_root / ".env").write_text("TOKEN=secret\n")
    (input_root / ".env.local").write_text("TOKEN=more-secret\n")
    destination = tmp_path / "runs" / "copy"

    result = provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=_workspace(destination, project_root=tmp_path),
        ),
    )

    assert result == destination.resolve()
    assert (result / "candidate.py").read_text() == "VALUE = 1\n"
    assert not (result / "workspace").exists()
    assert not (result / "logs").exists()
    assert not (result / ".git").exists()
    assert not Project.is_state_initialized(result)
    assert not (result / "agent.toml").exists()
    assert not list(result.rglob(".env*"))
    normalized = InputManifest.model_validate(
        tomllib.loads((result / "vibesys.input.toml").read_text())
    )
    assert normalized.workspace is None
    assert normalized.accuracy.timeout_seconds == 10
    assert normalized.benchmark.timeout_seconds == 20
    assert normalized.benchmark.result is not None
    assert normalized.benchmark.result.metric == "throughput"
    assert Project.is_state_initialized(input_root)
    assert (input_root / "agent.toml").is_file()


def test_provision_project_preserves_modal_entrypoint(tmp_path: Path) -> None:
    input_root = _write_input(
        tmp_path / "input",
        '\n[environment.modal]\nentrypoint = "deploy/service.py"\n',
    )
    entrypoint = input_root / "deploy" / "service.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text("app = object()\n")
    destination = tmp_path / "runs" / "copy"

    provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=_workspace(destination, project_root=tmp_path),
        ),
    )

    manifest = InputManifest.model_validate(
        tomllib.loads((destination / "vibesys.input.toml").read_text())
    )
    assert manifest.environment is not None
    assert manifest.environment.modal is not None
    assert manifest.environment.modal.entrypoint == "deploy/service.py"
    assert load_input_bundle(destination).modal_entrypoint == "deploy/service.py"


def test_provision_project_copies_and_rewrites_external_evaluator(tmp_path: Path) -> None:
    input_root = _write_input(
        tmp_path / "input",
        '\n[evaluator]\nsource = "../../evaluators/queue"\n',
    )
    evaluator = tmp_path / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    (evaluator / "check.py").write_text("print('ok')\n")
    (evaluator / ".env.evaluator").write_text("TOKEN=secret\n")
    destination = tmp_path / "runs" / "copy"

    provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=_workspace(destination, project_root=tmp_path),
            evaluator_source=evaluator,
        ),
    )

    assert (destination / "_evaluator" / "queue" / "check.py").is_file()
    assert not (destination / "_evaluator" / "queue" / ".env.evaluator").exists()
    manifest = InputManifest.model_validate(
        tomllib.loads((destination / "vibesys.input.toml").read_text())
    )
    assert manifest.evaluator is not None
    assert manifest.evaluator.source == "_evaluator/queue"
    loaded = load_input_bundle(destination)
    assert loaded.evaluator_path == (destination / "_evaluator" / "queue").resolve()


def test_provision_project_relocates_bundle_local_evaluator(tmp_path: Path) -> None:
    input_root = _write_input(
        tmp_path / "input",
        '\n[evaluator]\nsource = "checks/trusted"\n',
    )
    evaluator = input_root / "checks" / "trusted"
    evaluator.mkdir(parents=True)
    (evaluator / "check.py").write_text("print('ok')\n")
    destination = tmp_path / "runs" / "copy"

    provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=_workspace(destination, project_root=tmp_path),
            evaluator_source=evaluator,
        ),
    )

    assert not (destination / "checks" / "trusted").exists()
    assert (destination / "_evaluator" / "trusted" / "check.py").is_file()


def test_provision_project_materializes_git_source_without_nested_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source-repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    (repository / "library.py").write_text("VALUE = 2\n")
    Project.open(repository).state.create_project("source repository")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "source",
    )
    commit = _git(repository, "rev-parse", "HEAD")
    source = WorkspaceSource(
        name="library",
        repo=str(repository),
        commit=commit,
        dest="library",
    )
    input_root = _write_input(
        tmp_path / "input",
        (
            "\n[[workspace.sources]]\n"
            'name = "library"\n'
            f'repo = "{repository}"\n'
            f'commit = "{commit}"\n'
            'dest = "library"\n'
        ),
    )
    destination = tmp_path / "runs" / "copy"

    provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=_workspace(destination, project_root=tmp_path),
            workspace_sources=(source,),
        ),
    )

    assert (destination / "library" / "library.py").is_file()
    assert not (destination / "library" / ".git").exists()
    assert not Project.is_state_initialized(destination / "library")
    manifest = InputManifest.model_validate(
        tomllib.loads((destination / "vibesys.input.toml").read_text())
    )
    assert manifest.workspace is None


@pytest.mark.parametrize("destination_kind", ["same", "child"])
def test_provision_project_rejects_destination_in_source(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    input_root = _write_input(tmp_path / "input")
    destination = input_root if destination_kind == "same" else input_root / "runs" / "copy"

    with pytest.raises(ProjectProvisioningError, match="outside the input project"):
        provision_project(
            input_root,
            destination,
            spec=ProjectProvisioningSpec(workspace=_workspace(destination, project_root=tmp_path)),
        )

    assert (input_root / "candidate.py").read_text() == "VALUE = 1\n"
    assert not (input_root / "runs").exists()


def test_provision_project_preserves_existing_destination(tmp_path: Path) -> None:
    input_root = _write_input(tmp_path / "input")
    destination = tmp_path / "runs" / "copy"
    destination.mkdir(parents=True)
    marker = destination / "owned.txt"
    marker.write_text("keep\n")

    with pytest.raises(ProjectProvisioningError, match="already exists"):
        provision_project(
            input_root,
            destination,
            spec=ProjectProvisioningSpec(workspace=_workspace(destination, project_root=tmp_path)),
        )

    assert marker.read_text() == "keep\n"


def test_provision_project_cleans_partial_destination_after_collision(tmp_path: Path) -> None:
    repository = tmp_path / "source-repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    (repository / "value.py").write_text("VALUE = 1\n")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "source",
    )
    commit = _git(repository, "rev-parse", "HEAD")
    source = WorkspaceSource(
        name="library",
        repo=str(repository),
        commit=commit,
        dest="library",
    )
    input_root = _write_input(
        tmp_path / "input",
        (
            "\n[[workspace.sources]]\n"
            'name = "library"\n'
            f'repo = "{repository}"\n'
            f'commit = "{commit}"\n'
            'dest = "library"\n'
        ),
    )
    (input_root / "library").mkdir()
    destination = tmp_path / "runs" / "copy"

    with pytest.raises(ValueError, match=r"same paths: library"):
        provision_project(
            input_root,
            destination,
            spec=ProjectProvisioningSpec(
                workspace=_workspace(destination, project_root=tmp_path),
                workspace_sources=(source,),
            ),
        )

    assert not destination.exists()


def test_provision_project_requires_resolved_inputs_to_match_manifest(tmp_path: Path) -> None:
    input_root = _write_input(
        tmp_path / "input",
        """
[[workspace.sources]]
name = "library"
repo = "https://example.invalid/library.git"
commit = "0123456"
dest = "library"
""",
    )
    destination = tmp_path / "runs" / "copy"

    with pytest.raises(ProjectProvisioningError, match="source declarations"):
        provision_project(
            input_root,
            destination,
            spec=ProjectProvisioningSpec(workspace=_workspace(destination, project_root=tmp_path)),
        )

    assert not destination.exists()


def test_provision_project_applies_input_overlay_excludes(tmp_path: Path) -> None:
    input_root = _write_input(tmp_path / "input")
    model_cache = input_root / "model"
    model_cache.mkdir()
    (model_cache / "weights.bin").write_bytes(b"large")
    destination = tmp_path / "runs" / "copy"

    provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=_workspace(destination, project_root=tmp_path),
            input_excludes=frozenset({"model"}),
        ),
    )

    assert not (destination / "model").exists()
    assert (destination / "candidate.py").is_file()


def test_provision_project_preserves_repository_task_configuration(tmp_path: Path) -> None:
    source = tmp_path / "source"
    task = source / ".vibesys" / "tasks" / "latency"
    task.mkdir(parents=True)
    (source / "src").mkdir()
    (source / "src" / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
    (task / "OBJECTIVE.md").write_text("Reduce latency.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text(
        """version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('1')"]
""",
        encoding="utf-8",
    )
    (source / ".vibesys" / "notes.toml").write_text(
        "schema_version = 1\n",
        encoding="utf-8",
    )
    state = source / ".vibesys" / "state" / "local"
    state.mkdir(parents=True)
    (state / "secret.json").write_text("{}\n", encoding="utf-8")
    destination = tmp_path / "destination"

    provision_project(
        source,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=_workspace(destination, project_root=tmp_path),
            task_name="latency",
            input_project_dir=source,
        ),
    )

    assert (destination / "src" / "server.py").is_file()
    assert (destination / ".vibesys" / "tasks" / "latency" / "OBJECTIVE.md").is_file()
    assert (destination / ".vibesys" / "notes.toml").is_file()
    assert not (destination / ".vibesys" / "state").exists()
    assert not (destination / "vibesys.input.toml").exists()
