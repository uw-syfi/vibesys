from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from vibesys import input_project
from vibesys.input_project import (
    InputProjectError,
    discover_input_project,
    materialize_input_project,
)

if TYPE_CHECKING:
    from pathlib import Path


def _copy_dir(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)


def test_discover_input_project_finds_pyproject_next_to_reference(tmp_path: Path) -> None:
    input_dir = tmp_path / "queue-spsc"
    reference_dir = input_dir / "reference"
    reference_dir.mkdir(parents=True)
    (input_dir / "pyproject.toml").write_text("[project]\nname = 'queue-spsc-input'\n")

    assert discover_input_project(reference_dir) == input_dir
    assert discover_input_project(input_dir) is None
    assert discover_input_project(None) is None


def test_materialize_input_project_copies_and_rewrites_explicit_sdk_path_deps(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    input_core = project_root / "sdk" / "queue-input-core"
    input_core.mkdir(parents=True)
    (input_core / "pyproject.toml").write_text(
        "[project]\nname = 'queue-input-core'\nversion = '0.1.0'\n"
    )
    (input_core / "core.py").write_text("VALUE = 1\n")

    input_dir = project_root / "examples" / "data-structures" / "queue-spsc"
    input_dir.mkdir(parents=True)
    source_pyproject = (
        "[project]\n"
        "name = 'queue-spsc-input'\n"
        "version = '0.1.0'\n"
        "dependencies = ['queue-input-core']\n"
        "\n"
        "[tool.uv.sources]\n"
        "queue-input-core = { path = '../../../sdk/queue-input-core', editable = true }\n"
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

    assert [dependency.name for dependency in dependencies] == ["queue-input-core"]
    assert (workspace / "_input_libs" / "queue-input-core" / "core.py").read_text() == "VALUE = 1\n"
    assert (
        "queue-input-core = { path = '_input_libs/queue-input-core', editable = true }\n"
        in (workspace / "pyproject.toml").read_text()
    )
    assert (input_dir / "pyproject.toml").read_text() == source_pyproject


def test_materialize_input_project_copies_transitive_sdk_deps(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    sdk = project_root / "sdk"
    common = sdk / "queue-common"
    input_core = sdk / "queue-input-core"
    common.mkdir(parents=True)
    input_core.mkdir()
    (common / "pyproject.toml").write_text("[project]\nname = 'queue-common'\nversion = '0.1.0'\n")
    (input_core / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'queue-input-core'\n"
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
        "dependencies = ['queue-input-core']\n"
        "\n"
        "[tool.uv.sources]\n"
        "queue-input-core = { path = '../../../sdk/queue-input-core', editable = true }\n"
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
        "queue-input-core",
    ]
    assert (workspace / "_input_libs" / "queue-common" / "pyproject.toml").is_file()
    input_core_pyproject = workspace / "_input_libs" / "queue-input-core" / "pyproject.toml"
    assert "queue-common = { path = '../queue-common', editable = true }\n" in (
        input_core_pyproject.read_text()
    )


def test_materialize_input_project_rejects_path_deps_outside_sdk(tmp_path: Path) -> None:
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

    with pytest.raises(InputProjectError, match="outside sdk/"):
        materialize_input_project(
            input_dir,
            tmp_path / "workspace",
            project_root=project_root,
            copy_dir=_copy_dir,
        )


def test_materialize_input_project_uses_packaged_sdk_without_a_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "installed"
    packaged_sdk = tmp_path / "site-packages" / "vibesys" / "_sdk"
    vs_bench = packaged_sdk / "vs-bench"
    (vs_bench / "src" / "vs_bench").mkdir(parents=True)
    (vs_bench / "pyproject.toml").write_text("[project]\nname = 'vs-bench'\nversion = '0.1.0'\n")
    (vs_bench / "src" / "vs_bench" / "__init__.py").write_text("")

    input_dir = project_root / "examples" / "model-serving" / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'model-serving-input'\n"
        "version = '0.1.0'\n"
        "dependencies = ['vs-bench']\n"
        "\n"
        "[tool.uv.sources]\n"
        "vs-bench = { path = '../../../sdk/vs-bench' }\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(input_project, "packaged_sdk_root", lambda: packaged_sdk)

    dependencies = materialize_input_project(
        input_dir,
        workspace,
        project_root=project_root,
        copy_dir=_copy_dir,
    )

    assert [dependency.name for dependency in dependencies] == ["vs-bench"]
    assert (workspace / "_input_libs" / "vs-bench" / "src" / "vs_bench").is_dir()
    assert (
        "vs-bench = { path = '_input_libs/vs-bench' }" in (workspace / "pyproject.toml").read_text()
    )
