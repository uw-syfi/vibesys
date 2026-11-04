"""Contracts for provisioning a copied directory as a canonical project."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.real_git import REAL_GIT
from tests.support.task_project import task_path, write_task

from vibesys.inputs import InputManifest, WorkspaceSource, load_task
from vibesys.run import (
    ProjectProvisioningError,
    ProjectProvisioningSpec,
    provision_project,
)
from vibesys.run.workspace_policy import create_project_materializer
from vs_project.api import InvalidTaskDefinitionError, Project, ProjectLayoutError
from vs_runtime.api.testing import unconfined_host_environment
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import ProjectMaterializer


def _materializer(destination: Path) -> ProjectMaterializer:
    return create_project_materializer(
        destination,
        environment=unconfined_host_environment(),
        backend=FakeComputeBackend(threads=SimThreads()),
        log=lambda _message: None,
        git=REAL_GIT,
    )


_TASK = "default"


def _write_input(root: Path, extra_manifest: str = "") -> Path:
    """Write a one-task project whose candidate source is ``candidate.py``."""
    root.mkdir(parents=True)
    (root / "candidate.py").write_text("VALUE = 1\n")
    write_task(
        root,
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
        + extra_manifest,
        objective="Make the candidate faster.\n",
    )
    return root


def _destination_manifest(destination: Path) -> InputManifest:
    return InputManifest.model_validate(
        tomllib.loads((task_path(destination) / "vibesys.input.toml").read_text())
    )


def _git(cwd: Path, *args: str) -> str:
    return run_test_command(
        ["git", *args],
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
    Project.open(input_root).state.create_project("input", now=datetime(2026, 1, 1, tzinfo=UTC))
    (input_root / "agent.toml").write_text("[model]\nname = 'private'\n")
    (input_root / ".env").write_text("TOKEN=secret\n")
    (input_root / ".env.local").write_text("TOKEN=more-secret\n")
    destination = tmp_path / "runs" / "copy"

    result = provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            materializer=_materializer(destination),
            task_name=_TASK,
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
    normalized = _destination_manifest(result)
    assert not (result / "vibesys.input.toml").exists()
    assert normalized.accuracy.timeout_seconds == 10
    assert normalized.benchmark.timeout_seconds == 20
    assert normalized.benchmark.result is not None
    assert normalized.benchmark.result.metric == "throughput"
    assert Project.is_state_initialized(input_root)
    assert (input_root / "agent.toml").is_file()


def test_provision_project_preserves_benchmark_result_protocol(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    write_task(
        input_root,
        """\
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "check.py"]

[benchmark]
command = ["python", "benchmark.py"]
result_protocol = 2
""",
    )
    destination = tmp_path / "runs" / "copy"

    _provision(input_root, destination)
    bundle = load_task(destination)

    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"


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
            materializer=_materializer(destination),
            task_name=_TASK,
        ),
    )

    manifest = _destination_manifest(destination)
    assert manifest.environment is not None
    assert manifest.environment.modal is not None
    assert manifest.environment.modal.entrypoint == "deploy/service.py"
    assert load_task(destination).modal_entrypoint == "deploy/service.py"


def test_provision_project_keeps_task_local_evaluator_in_its_task(tmp_path: Path) -> None:
    input_root = _write_input(
        tmp_path / "input",
        '\n[evaluator]\nsource = "queue"\n',
    )
    evaluator = task_path(input_root) / "queue"
    evaluator.mkdir()
    (evaluator / "check.py").write_text("print('ok')\n")
    (evaluator / ".env.evaluator").write_text("TOKEN=secret\n")
    destination = tmp_path / "runs" / "copy"

    _provision(input_root, destination)

    copied = task_path(destination) / "queue"
    assert (copied / "check.py").is_file()
    assert not (copied / ".env.evaluator").exists()
    assert not (destination / "_evaluator").exists()
    assert load_task(destination).evaluator_path == copied.resolve()


def test_provision_project_keeps_a_packaged_evaluator_declaration(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    write_task(
        input_root,
        """\
version = 1

[agent]
domain = "generic"

[evaluator]
name = "vibesys-evaluator-request-factory"
version = "0.1.0"

[accuracy]
command = ["python", "check.py"]
timeout_seconds = 10

[benchmark]
entrypoint = "request-factory-adapter"
args = ["benchmark/run.py"]
timeout_seconds = 20

[benchmark.result]
json_argument = "--output-json"
metric = "throughput"
""",
    )
    destination = tmp_path / "runs" / "copy"

    _provision(input_root, destination)

    # Resume re-reads this manifest; it must still validate.
    manifest = _destination_manifest(destination)
    assert manifest.evaluator is not None
    assert manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert manifest.evaluator.version == "0.1.0"


def test_provision_project_materializes_git_source_without_nested_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source-repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    (repository / "library.py").write_text("VALUE = 2\n")
    Project.open(repository).state.create_project(
        "source repository", now=datetime(2026, 1, 1, tzinfo=UTC)
    )
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
            materializer=_materializer(destination),
            task_name=_TASK,
            workspace_sources=(source,),
        ),
    )

    assert (destination / "library" / "library.py").is_file()
    assert not (destination / "library" / ".git").exists()
    assert not Project.is_state_initialized(destination / "library")


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
            spec=ProjectProvisioningSpec(materializer=_materializer(destination), task_name=_TASK),
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
            spec=ProjectProvisioningSpec(materializer=_materializer(destination), task_name=_TASK),
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
                materializer=_materializer(destination),
                task_name=_TASK,
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
            spec=ProjectProvisioningSpec(materializer=_materializer(destination), task_name=_TASK),
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
            materializer=_materializer(destination),
            task_name=_TASK,
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
            materializer=_materializer(destination),
            task_name="latency",
            input_project_dir=source,
        ),
    )

    assert (destination / "src" / "server.py").is_file()
    assert (destination / ".vibesys" / "tasks" / "latency" / "OBJECTIVE.md").is_file()
    assert (destination / ".vibesys" / "notes.toml").is_file()
    assert not (destination / ".vibesys" / "state").exists()
    assert not (destination / "vibesys.input.toml").exists()


def _provision(
    input_root: Path,
    destination: Path,
    *,
    workspace_sources: tuple[WorkspaceSource, ...] = (),
) -> Path:
    return provision_project(
        input_root,
        destination,
        spec=ProjectProvisioningSpec(
            materializer=_materializer(destination),
            task_name=_TASK,
            workspace_sources=workspace_sources,
        ),
    )


def test_provision_project_rejects_missing_input(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ProjectProvisioningError, match="input project does not exist") as info:
        _provision(missing, tmp_path / "copy")

    assert str(missing.resolve()) in str(info.value)


def test_provision_project_rejects_input_that_is_a_file(tmp_path: Path) -> None:
    input_file = tmp_path / "input.txt"
    input_file.write_text("x")

    with pytest.raises(ProjectProvisioningError, match="input project is not a directory"):
        _provision(input_file, tmp_path / "copy")


def test_provision_project_rejects_a_root_manifest_project_without_tasks(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "OBJECTIVE.md").write_text("Make the candidate faster.\n")
    (input_root / "vibesys.input.toml").write_text("version = 1\n")

    with pytest.raises(ProjectLayoutError):
        _provision(input_root, tmp_path / "copy")

    assert not (tmp_path / "copy").exists()


def test_provision_project_rejects_workspace_rooted_elsewhere(tmp_path: Path) -> None:
    input_root = _write_input(tmp_path / "input")
    destination = tmp_path / "runs" / "copy"
    other_root = tmp_path / "runs" / "other"

    with pytest.raises(ProjectProvisioningError, match="workspace root does not match") as info:
        provision_project(
            input_root,
            destination,
            spec=ProjectProvisioningSpec(materializer=_materializer(other_root), task_name=_TASK),
        )

    assert f"{other_root.resolve()} != {destination.resolve()}" in str(info.value)


def test_provision_project_requires_task_manifest(tmp_path: Path) -> None:
    input_root = _write_input(tmp_path / "input")
    (task_path(input_root) / "vibesys.input.toml").unlink()

    with pytest.raises(InvalidTaskDefinitionError, match=r"vibesys\.input\.toml"):
        _provision(input_root, tmp_path / "copy")

    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("contents", ["not = [valid", 'version = 99\nunknown_key = "x"\n'])
def test_provision_project_reports_invalid_manifest(tmp_path: Path, contents: str) -> None:
    input_root = _write_input(tmp_path / "input")
    (task_path(input_root) / "vibesys.input.toml").write_text(contents)

    with pytest.raises(ProjectProvisioningError, match="invalid input manifest"):
        _provision(input_root, tmp_path / "copy")

    assert not (tmp_path / "copy").exists()


def test_provision_project_requires_git_metadata_stripped_from_sources(tmp_path: Path) -> None:
    input_root = _write_input(
        tmp_path / "input",
        """
[[workspace.sources]]
name = "library"
repo = "https://example.invalid/library.git"
commit = "0123456"
dest = "library"
strip_git = false
""",
    )
    source = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="library",
        strip_git=False,
    )

    with pytest.raises(ProjectProvisioningError, match="strip_git = true"):
        _provision(input_root, tmp_path / "copy", workspace_sources=(source,))

    assert not (tmp_path / "copy").exists()
