from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    InputProjectError,
    SDKRoots,
    materialize_input_project,
)

if TYPE_CHECKING:
    from pathlib import Path


def _copy_directory(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _write_project(project_dir: Path, name: str, *, sources: str = "") -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text(
        f"[project]\nname = '{name}'\nversion = '0.1.0'\n{sources}"
    )


def test_materialize_copies_sdk_dependency_and_rewrites_only_workspace_file(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    sdk_root = project_root / "sdk"
    dependency = sdk_root / "queue-input-core"
    _write_project(dependency, "queue-input-core")
    (dependency / "core.py").write_text("VALUE = 1\n")
    input_project = project_root / "examples" / "data-structures" / "queue-spsc"
    _write_project(
        input_project,
        "queue-spsc-input",
        sources=(
            "\n[tool.uv.sources]\n"
            "queue-input-core = { path = '../../../sdk/queue-input-core', editable = true }\n"
        ),
    )
    original = (input_project / "pyproject.toml").read_text()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    dependencies = materialize_input_project(
        input_project,
        workspace,
        sdk_roots=SDKRoots(checkout=sdk_root, packaged=None),
        copy_directory=_copy_directory,
    )

    assert [(item.name, item.workspace_path.name) for item in dependencies] == [
        ("queue-input-core", "queue-input-core")
    ]
    assert (workspace / "_input_libs" / "queue-input-core" / "core.py").read_text() == (
        "VALUE = 1\n"
    )
    assert (
        "queue-input-core = { path = '_input_libs/queue-input-core', editable = true }\n"
        in (workspace / "pyproject.toml").read_text()
    )
    assert (input_project / "pyproject.toml").read_text() == original


def test_materialize_copies_transitive_sdk_dependencies(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    sdk_root = project_root / "sdk"
    common = sdk_root / "queue-common"
    input_core = sdk_root / "queue-input-core"
    _write_project(common, "queue-common")
    _write_project(
        input_core,
        "queue-input-core",
        sources=(
            "\n[tool.uv.sources]\nqueue-common = { path = '../queue-common', editable = true }\n"
        ),
    )
    input_project = project_root / "examples" / "data-structures" / "queue-mpsc"
    _write_project(
        input_project,
        "queue-mpsc-input",
        sources=(
            "\n[tool.uv.sources]\n"
            "queue-input-core = { path = '../../../sdk/queue-input-core', editable = true }\n"
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    dependencies = materialize_input_project(
        input_project,
        workspace,
        sdk_roots=SDKRoots(checkout=sdk_root, packaged=None),
        copy_directory=_copy_directory,
    )

    assert [dependency.name for dependency in dependencies] == [
        "queue-common",
        "queue-input-core",
    ]
    copied_core = workspace / "_input_libs" / "queue-input-core" / "pyproject.toml"
    assert (
        "queue-common = { path = '../queue-common', editable = true }\n" in copied_core.read_text()
    )


def test_materialize_resolves_a_packaged_sdk_without_checkout(tmp_path: Path) -> None:
    packaged_sdk = tmp_path / "site-packages" / "vibesys" / "_sdk"
    package = packaged_sdk / "vs-bench"
    _write_project(package, "vs-bench")
    input_project = tmp_path / "installed" / "examples" / "model-serving" / "input"
    _write_project(
        input_project,
        "model-serving-input",
        sources="\n[tool.uv.sources]\nvs-bench = { path = '../../../sdk/vs-bench' }\n",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    dependencies = materialize_input_project(
        input_project,
        workspace,
        sdk_roots=SDKRoots(checkout=tmp_path / "missing" / "sdk", packaged=packaged_sdk),
        copy_directory=_copy_directory,
    )

    assert [dependency.name for dependency in dependencies] == ["vs-bench"]
    assert (workspace / "_input_libs" / "vs-bench" / "pyproject.toml").is_file()
    assert (
        "vs-bench = { path = '_input_libs/vs-bench' }" in (workspace / "pyproject.toml").read_text()
    )


@pytest.mark.parametrize("raw_path", ["../../../not-a-library", "../../../sdk/../outside"])
def test_materialize_rejects_dependencies_outside_owned_sdk_roots(
    tmp_path: Path, raw_path: str
) -> None:
    input_project = tmp_path / "repo" / "examples" / "data-structures" / "queue-spsc"
    _write_project(
        input_project,
        "queue-spsc-input",
        sources=f"\n[tool.uv.sources]\nbad-local = {{ path = '{raw_path}' }}\n",
    )

    with pytest.raises(InputProjectError, match="outside sdk/"):
        materialize_input_project(
            input_project,
            tmp_path / "workspace",
            sdk_roots=SDKRoots(checkout=tmp_path / "repo" / "sdk", packaged=None),
            copy_directory=_copy_directory,
        )
