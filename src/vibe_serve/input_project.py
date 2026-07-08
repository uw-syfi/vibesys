"""Materialize Python-backed input project dependencies.

Input bundles may be tiny Python projects whose ``pyproject.toml`` declares
path dependencies on reusable input-only libraries under ``examples/libs``.
Runs copy inputs into an isolated experiment workspace, so repo-relative paths
must be rewritten to copied workspace-local paths before agents execute
``uv run``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class InputProjectError(ValueError):
    """Raised when an input project's local dependencies cannot be materialized."""


@dataclass(frozen=True)
class InputDependency:
    name: str
    source_path: Path
    workspace_path: Path


CopyDir = Callable[[Path, Path], None]
LogFn = Callable[[str], None]


def discover_input_project(reference_dir: Path | None) -> Path | None:
    """Return the input project directory for a conventional ``reference/`` path."""

    if reference_dir is None or reference_dir.name != "reference":
        return None
    candidate = reference_dir.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def materialize_input_project(
    input_project_dir: Path,
    workspace: Path,
    *,
    project_root: Path,
    copy_dir: CopyDir,
    log: LogFn | None = None,
) -> list[InputDependency]:
    """Copy an input ``pyproject.toml`` and its ``examples/libs`` path deps.

    The source input project's ``pyproject.toml`` remains repo-relative. The
    workspace copy is rewritten so every explicit ``examples/libs`` path dep
    points at ``workspace/_input_libs/<relative-lib-path>``.
    """

    pyproject = input_project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return []

    examples_libs = (project_root / "examples" / "libs").resolve()
    dependencies = _collect_examples_lib_dependencies(input_project_dir, examples_libs)
    mapping = {
        dep_path: workspace / "_input_libs" / dep_path.relative_to(examples_libs)
        for dep_path in dependencies
    }

    for source_path, workspace_path in sorted(mapping.items(), key=lambda item: str(item[0])):
        copy_dir(source_path, workspace_path)
        copied_pyproject = workspace_path / "pyproject.toml"
        if copied_pyproject.is_file():
            _rewrite_pyproject_in_place(
                copied_pyproject,
                source_project_dir=source_path,
                workspace_project_dir=workspace_path,
                copied_libs=mapping,
            )
        if log:
            log(
                "[input] copied local input dependency "
                f"{source_path.relative_to(project_root)} -> {workspace_path.relative_to(workspace)}"
            )

    workspace_pyproject = workspace / "pyproject.toml"
    workspace_pyproject.write_text(
        _rewrite_pyproject_text(
            pyproject.read_text(),
            source_project_dir=input_project_dir,
            workspace_project_dir=workspace,
            copied_libs=mapping,
        )
    )

    return [
        InputDependency(
            name=_project_name(source_path),
            source_path=source_path,
            workspace_path=workspace_path,
        )
        for source_path, workspace_path in sorted(mapping.items(), key=lambda item: str(item[0]))
    ]


def _collect_examples_lib_dependencies(project_dir: Path, examples_libs: Path) -> set[Path]:
    collected: set[Path] = set()
    visiting: set[Path] = set()

    def visit(current: Path) -> None:
        current = current.resolve()
        if current in visiting:
            raise InputProjectError(f"Cyclic input dependency involving {current}")
        visiting.add(current)
        try:
            for dep_name, raw_path in _path_sources(current).items():
                dep_path = (current / raw_path).resolve()
                if not _is_relative_to(dep_path, examples_libs):
                    raise InputProjectError(
                        f"Input dependency {dep_name!r} points outside examples/libs: {raw_path}"
                    )
                if not (dep_path / "pyproject.toml").is_file():
                    raise InputProjectError(
                        f"Input dependency {dep_name!r} has no pyproject.toml: {dep_path}"
                    )
                if dep_path not in collected:
                    collected.add(dep_path)
                    visit(dep_path)
        finally:
            visiting.remove(current)

    visit(project_dir)
    return collected


def _path_sources(project_dir: Path) -> dict[str, str]:
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    data = tomllib.loads(pyproject.read_text())
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    result: dict[str, str] = {}
    for name, spec in sources.items():
        if isinstance(spec, dict) and isinstance(spec.get("path"), str):
            result[name] = spec["path"]
    return result


def _rewrite_pyproject_in_place(
    pyproject: Path,
    *,
    source_project_dir: Path,
    workspace_project_dir: Path,
    copied_libs: dict[Path, Path],
) -> None:
    pyproject.write_text(
        _rewrite_pyproject_text(
            pyproject.read_text(),
            source_project_dir=source_project_dir,
            workspace_project_dir=workspace_project_dir,
            copied_libs=copied_libs,
        )
    )


def _rewrite_pyproject_text(
    text: str,
    *,
    source_project_dir: Path,
    workspace_project_dir: Path,
    copied_libs: dict[Path, Path],
) -> str:
    replacements: dict[str, tuple[str, str]] = {}
    for source_name, raw_path in _path_sources_from_text(text).items():
        resolved = (source_project_dir / raw_path).resolve()
        copied_path = copied_libs.get(resolved)
        if copied_path is None:
            continue
        rewritten = os.path.relpath(copied_path, workspace_project_dir).replace(os.sep, "/")
        replacements[source_name] = (raw_path, rewritten)

    if not replacements:
        return text

    lines = text.splitlines(keepends=True)
    output: list[str] = []
    in_uv_sources = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_uv_sources = stripped == "[tool.uv.sources]"

        if in_uv_sources:
            for source_name, (old_path, new_path) in replacements.items():
                if re.match(rf"\s*{re.escape(source_name)}\s*=", line):
                    line = re.sub(
                        rf"(path\s*=\s*['\"]){re.escape(old_path)}(['\"])",
                        lambda match, replacement=new_path: (
                            f"{match.group(1)}{replacement}{match.group(2)}"
                        ),
                        line,
                    )
                    break
        output.append(line)
    return "".join(output)


def _path_sources_from_text(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    return {
        name: spec["path"]
        for name, spec in sources.items()
        if isinstance(spec, dict) and isinstance(spec.get("path"), str)
    }


def _project_name(project_dir: Path) -> str:
    data = tomllib.loads((project_dir / "pyproject.toml").read_text())
    name = data.get("project", {}).get("name")
    return str(name) if name else project_dir.name


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
