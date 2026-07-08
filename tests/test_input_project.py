from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vibe_serve.input_project import (
    InputProjectError,
    discover_input_project,
    materialize_input_project,
)


def _copy_dir(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)


def test_discover_input_project_finds_pyproject_next_to_reference(tmp_path):
    input_dir = tmp_path / "queue-spsc"
    reference_dir = input_dir / "reference"
    reference_dir.mkdir(parents=True)
    (input_dir / "pyproject.toml").write_text("[project]\nname = 'queue-spsc-input'\n")

    assert discover_input_project(reference_dir) == input_dir
    assert discover_input_project(input_dir) is None
    assert discover_input_project(None) is None


def test_materialize_input_project_copies_and_rewrites_explicit_lib_path_deps(tmp_path):
    project_root = tmp_path / "project"
    harness = project_root / "examples" / "libs" / "queue-harness"
    harness.mkdir(parents=True)
    (harness / "pyproject.toml").write_text(
        "[project]\nname = 'queue-harness'\nversion = '0.1.0'\n"
    )
    (harness / "harness.py").write_text("VALUE = 1\n")

    input_dir = project_root / "examples" / "data-structures" / "queue-spsc"
    input_dir.mkdir(parents=True)
    source_pyproject = (
        "[project]\n"
        "name = 'queue-spsc-input'\n"
        "version = '0.1.0'\n"
        "dependencies = ['queue-harness']\n"
        "\n"
        "[tool.uv.sources]\n"
        "queue-harness = { path = '../../libs/queue-harness', editable = true }\n"
    )
    (input_dir / "pyproject.toml").write_text(source_pyproject)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    dependencies = materialize_input_project(
        input_dir,
        workspace,
        project_root=project_root,
        copy_dir=_copy_dir,
    )

    assert [dependency.name for dependency in dependencies] == ["queue-harness"]
    assert (workspace / "_input_libs" / "queue-harness" / "harness.py").read_text() == "VALUE = 1\n"
    assert (
        "queue-harness = { path = '_input_libs/queue-harness', editable = true }\n"
        in (workspace / "pyproject.toml").read_text()
    )
    assert (input_dir / "pyproject.toml").read_text() == source_pyproject


def test_materialize_input_project_copies_transitive_examples_lib_deps(tmp_path):
    project_root = tmp_path / "project"
    libs = project_root / "examples" / "libs"
    common = libs / "queue-common"
    harness = libs / "queue-harness"
    common.mkdir(parents=True)
    harness.mkdir()
    (common / "pyproject.toml").write_text("[project]\nname = 'queue-common'\nversion = '0.1.0'\n")
    (harness / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'queue-harness'\n"
        "version = '0.1.0'\n"
        "dependencies = ['queue-common']\n"
        "\n"
        "[tool.uv.sources]\n"
        "queue-common = { path = '../queue-common', editable = true }\n"
    )

    input_dir = project_root / "examples" / "data-structures" / "queue-mpsc"
    input_dir.mkdir(parents=True)
    (input_dir / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'queue-mpsc-input'\n"
        "version = '0.1.0'\n"
        "dependencies = ['queue-harness']\n"
        "\n"
        "[tool.uv.sources]\n"
        "queue-harness = { path = '../../libs/queue-harness', editable = true }\n"
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    dependencies = materialize_input_project(
        input_dir,
        workspace,
        project_root=project_root,
        copy_dir=_copy_dir,
    )

    assert [dependency.name for dependency in dependencies] == [
        "queue-common",
        "queue-harness",
    ]
    assert (workspace / "_input_libs" / "queue-common" / "pyproject.toml").is_file()
    harness_pyproject = workspace / "_input_libs" / "queue-harness" / "pyproject.toml"
    assert "queue-common = { path = '../queue-common', editable = true }\n" in (
        harness_pyproject.read_text()
    )


def test_materialize_input_project_rejects_path_deps_outside_examples_libs(tmp_path):
    project_root = tmp_path / "project"
    input_dir = project_root / "examples" / "data-structures" / "queue-spsc"
    input_dir.mkdir(parents=True)
    (input_dir / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'queue-spsc-input'\n"
        "version = '0.1.0'\n"
        "\n"
        "[tool.uv.sources]\n"
        "bad-local = { path = '../../../not-a-library' }\n"
    )

    with pytest.raises(InputProjectError, match="outside examples/libs"):
        materialize_input_project(
            input_dir,
            tmp_path / "workspace",
            project_root=project_root,
            copy_dir=_copy_dir,
        )
