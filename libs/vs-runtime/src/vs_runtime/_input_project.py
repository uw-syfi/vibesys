"""Materialize SDK path dependencies declared by an input project."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vs_runtime._sdk_paths import (
    InputProjectError,
    SDKRoots,
    relative_sdk_source,
    resolve_sdk_source,
)


@dataclass(frozen=True, slots=True)
class InputDependency:
    """One SDK project copied into the run workspace."""

    name: str
    source_path: Path
    workspace_path: Path


CopyDirectory = Callable[[Path, Path], None]
Log = Callable[[str], None]


def materialize_input_project(
    input_project_dir: Path,
    workspace: Path,
    *,
    sdk_roots: SDKRoots,
    copy_directory: CopyDirectory,
    log: Log | None = None,
) -> list[InputDependency]:
    """Copy an input project's SDK path dependencies and rewrite local paths.

    Only path dependencies resolving inside the supplied checkout or packaged
    SDK roots are accepted. The source project remains unchanged; its workspace
    copy points to the materialized ``_input_libs`` tree.
    """
    pyproject = input_project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return []

    dependencies = _collect_sdk_dependencies(input_project_dir, roots=sdk_roots)
    mapping = {
        dep_path: workspace
        / "_input_libs"
        / relative_sdk_source(
            dep_path,
            checkout_sdk_root=sdk_roots.checkout,
            packaged_sdk_root=sdk_roots.packaged,
        )
        for dep_path in dependencies
    }

    for source_path, workspace_path in sorted(mapping.items(), key=lambda item: str(item[0])):
        copy_directory(source_path, workspace_path)
        copied_pyproject = workspace_path / "pyproject.toml"
        if copied_pyproject.is_file():
            _rewrite_pyproject_in_place(
                copied_pyproject,
                source_project_dir=source_path,
                workspace_project_dir=workspace_path,
                copied_libs=mapping,
                roots=sdk_roots,
            )
        if log:
            relative_source = relative_sdk_source(
                source_path,
                checkout_sdk_root=sdk_roots.checkout,
                packaged_sdk_root=sdk_roots.packaged,
            )
            log(
                "[input] copied local input dependency "
                f"sdk/{relative_source} -> {workspace_path.relative_to(workspace)}"
            )

    workspace_pyproject = workspace / "pyproject.toml"
    workspace_pyproject.write_text(
        _rewrite_pyproject_text(
            pyproject.read_text(),
            source_project_dir=input_project_dir,
            workspace_project_dir=workspace,
            copied_libs=mapping,
            roots=sdk_roots,
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


def _collect_sdk_dependencies(project_dir: Path, *, roots: SDKRoots) -> set[Path]:
    collected: set[Path] = set()
    visiting: set[Path] = set()

    def visit(current: Path) -> None:
        current = current.resolve()
        if current in visiting:
            message = f"Cyclic input dependency involving {current}"
            raise InputProjectError(message)
        visiting.add(current)
        try:
            for dep_name, raw_path in _path_sources(current).items():
                try:
                    dep_path = resolve_sdk_source(
                        current,
                        raw_path,
                        checkout_sdk_root=roots.checkout,
                        packaged_sdk_root=roots.packaged,
                    )
                except InputProjectError as exc:
                    raise InputProjectError.dependency(dep_name, exc) from exc
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
    return {
        name: spec["path"]
        for name, spec in sources.items()
        if isinstance(spec, dict) and isinstance(spec.get("path"), str)
    }


def _rewrite_pyproject_in_place(
    pyproject: Path,
    *,
    source_project_dir: Path,
    workspace_project_dir: Path,
    copied_libs: dict[Path, Path],
    roots: SDKRoots,
) -> None:
    pyproject.write_text(
        _rewrite_pyproject_text(
            pyproject.read_text(),
            source_project_dir=source_project_dir,
            workspace_project_dir=workspace_project_dir,
            copied_libs=copied_libs,
            roots=roots,
        )
    )


def _rewrite_pyproject_text(
    text: str,
    *,
    source_project_dir: Path,
    workspace_project_dir: Path,
    copied_libs: dict[Path, Path],
    roots: SDKRoots,
) -> str:
    replacements: dict[str, tuple[str, str]] = {}
    for source_name, raw_path in _path_sources_from_text(text).items():
        resolved = resolve_sdk_source(
            source_project_dir,
            raw_path,
            checkout_sdk_root=roots.checkout,
            packaged_sdk_root=roots.packaged,
        )
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
        rewritten_line = line
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_uv_sources = stripped == "[tool.uv.sources]"

        if in_uv_sources:
            for source_name, (old_path, new_path) in replacements.items():
                if re.match(rf"\s*{re.escape(source_name)}\s*=", line):
                    rewritten_line = re.sub(
                        rf"(path\s*=\s*['\"]){re.escape(old_path)}(['\"])",
                        lambda match, replacement=new_path: (
                            f"{match.group(1)}{replacement}{match.group(2)}"
                        ),
                        line,
                    )
                    break
        output.append(rewritten_line)
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


__all__ = ["InputDependency", "materialize_input_project"]
