#!/usr/bin/env python3
"""Fail when a library's declared dependencies disagree with what it imports.

Each `libs/*` uv workspace member declares its own dependencies in its
`pyproject.toml`. This check reads the runtime sources of every such member
(`<member>/src`) and fails when:

    1. A member imports a third-party package it does not declare.
    2. A member declares a third-party package its sources never import.
    3. A member imports another workspace member it does not declare, declares a
       member it never imports, or declares one without a
       `[tool.uv.sources] <name> = { workspace = true }` entry.
    4. A member imports a package owned by the root distribution (`src/`); a
       library never depends upward.
    5. A member's third-party dependency is missing from the root project's
       `dependencies`. The shipped `vibesys` wheel bundles every library's
       source, so its metadata must still name everything the libraries need.

Libraries that are not yet workspace members are not checked. The check is the
packaging-level counterpart of `tach check`, which only freezes import edges.
Test-only imports are out of scope: tests run from the root environment.

Usage:
    uv run python scripts/check_member_dependencies.py
    uv run python scripts/check_member_dependencies.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

LIBRARY_ROOT = "libs"
EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_TOOL_ERROR = 2
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def canonical_name(name: str) -> str:
    """Normalize a distribution name the way PEP 503 does."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_specifier(requirement: str) -> str:
    """Return a requirement's version constraint in a comparable form."""
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        message = f"not a valid requirement: {requirement!r}"
        raise ValueError(message)
    remainder = requirement[match.end() :].replace(" ", "")
    return ",".join(sorted(remainder.split(",")))


def requirement_name(requirement: str) -> str:
    """Return the canonical distribution name of a PEP 508 requirement string."""
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        message = f"not a valid requirement: {requirement!r}"
        raise ValueError(message)
    return canonical_name(match.group(1))


@dataclass(frozen=True)
class Member:
    """One library workspace member as declared and as written."""

    name: str
    directory: str
    dependencies: frozenset[str]
    workspace_sources: frozenset[str]
    imports: frozenset[str]
    packages: frozenset[str]
    specifiers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Workspace:
    """The members plus the facts about the rest of the repository they are checked against."""

    members: tuple[Member, ...]
    root_dependencies: frozenset[str]
    root_packages: frozenset[str]
    root_specifiers: Mapping[str, str] = field(default_factory=dict)


def distributions_for(import_name: str, installed: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """Return canonical names of the distributions that may provide ``import_name``."""
    provided = {canonical_name(dist) for dist in installed.get(import_name, ())}
    return frozenset({canonical_name(import_name), *provided})


def check_workspace(workspace: Workspace, installed: Mapping[str, Sequence[str]]) -> list[str]:
    """Return one message per disagreement between declarations and imports."""
    owner_of = {package: member.name for member in workspace.members for package in member.packages}
    member_names = {member.name for member in workspace.members}
    failures: list[str] = []
    for member in sorted(workspace.members, key=lambda item: item.name):
        failures.extend(_check_member(member, workspace, installed, owner_of, member_names))
    return failures


def _check_member(
    member: Member,
    workspace: Workspace,
    installed: Mapping[str, Sequence[str]],
    owner_of: Mapping[str, str],
    member_names: set[str],
) -> list[str]:
    where = member.directory
    failures: list[str] = []
    used_members: set[str] = set()
    used_third_party: set[str] = set()
    for import_name in sorted(member.imports - member.packages):
        if import_name in owner_of:
            used_members.add(owner_of[import_name])
        elif import_name in workspace.root_packages:
            failures.append(f"{where}: imports {import_name!r}, which the root distribution owns")
        elif declared := distributions_for(import_name, installed) & member.dependencies:
            used_third_party.update(declared)
        else:
            wanted = sorted(distributions_for(import_name, installed))[0]
            failures.append(
                f"{where}: imports {import_name!r} but does not declare {wanted!r} "
                "in [project] dependencies"
            )
    declared_members = member.dependencies & member_names
    failures.extend(
        f"{where}: imports workspace member {name!r} but does not declare it"
        for name in sorted(used_members - member.dependencies)
    )
    failures.extend(
        f"{where}: declares workspace member {name!r} but never imports it"
        for name in sorted(declared_members - used_members)
    )
    failures.extend(
        f"{where}: {name!r} needs [tool.uv.sources] {name} = {{ workspace = true }}"
        for name in sorted(declared_members - member.workspace_sources)
    )
    failures.extend(
        f"{where}: declares {name!r} but never imports it"
        for name in sorted(member.dependencies - member_names - used_third_party)
    )
    failures.extend(
        f"{where}: dependency {name!r} is missing from the root project's dependencies, "
        "so the bundled wheel would not install it"
        for name in sorted(used_third_party - workspace.root_dependencies)
    )
    failures.extend(
        f"{where}: declares {name!r} as {member.specifiers[name]!r} but the root project "
        f"declares {workspace.root_specifiers[name]!r}; keep one constraint"
        for name in sorted(
            used_third_party & member.specifiers.keys() & workspace.root_specifiers.keys()
        )
        if member.specifiers[name] != workspace.root_specifiers[name]
    )
    return failures


def imported_top_level_names(source_root: Path) -> frozenset[str]:
    """Return the absolute top-level module names imported anywhere under ``source_root``."""
    names: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.partition(".")[0])
    return frozenset(names - sys.stdlib_module_names)


def load_workspace(repo_root: Path) -> Workspace:
    """Read the root project and every `libs/*` workspace member under ``repo_root``."""
    root = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = root.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    directories = sorted(
        {
            path
            for pattern in patterns
            if pattern.startswith(f"{LIBRARY_ROOT}/")
            for path in repo_root.glob(pattern)
            if (path / "pyproject.toml").is_file()
        }
    )
    members = tuple(_load_member(repo_root, directory) for directory in directories)
    root_source = repo_root / "src"
    root_packages = (
        frozenset(
            path.name
            for path in root_source.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        if root_source.is_dir()
        else frozenset()
    )
    root_dependencies = frozenset(
        requirement_name(item) for item in root.get("project", {}).get("dependencies", [])
    )
    root_specifiers = {
        requirement_name(item): requirement_specifier(item)
        for item in root.get("project", {}).get("dependencies", [])
    }
    return Workspace(members, root_dependencies, root_packages, root_specifiers)


def _load_member(repo_root: Path, directory: Path) -> Member:
    data = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    source_root = directory / "src"
    packages = frozenset(
        path.name for path in source_root.iterdir() if path.is_dir() and path.name.isidentifier()
    )
    return Member(
        name=canonical_name(data["project"]["name"]),
        directory=directory.relative_to(repo_root).as_posix(),
        dependencies=frozenset(
            requirement_name(item) for item in data["project"].get("dependencies", [])
        ),
        workspace_sources=frozenset(
            canonical_name(name)
            for name, spec in sources.items()
            if isinstance(spec, dict) and spec.get("workspace") is True
        ),
        specifiers={
            requirement_name(item): requirement_specifier(item)
            for item in data["project"].get("dependencies", [])
        },
        imports=imported_top_level_names(source_root),
        packages=packages,
    )


def main() -> int:
    """Check every library member, returning a process exit code."""
    parser = argparse.ArgumentParser(description="Check libs/* members' declared dependencies.")
    parser.add_argument("--root", type=Path, default=Path(), help="Repository root (default: cwd)")
    args = parser.parse_args()
    try:
        workspace = load_workspace(args.root)
    except (OSError, KeyError, ValueError, SyntaxError, tomllib.TOMLDecodeError) as error:
        print(f"check_member_dependencies: cannot read the workspace: {error}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    failures = check_workspace(workspace, packages_distributions())
    if failures:
        print("Library dependency declarations disagree with the library sources:")
        for failure in failures:
            print(f"  {failure}")
        return EXIT_VIOLATIONS
    print(f"{len(workspace.members)} library workspace members declare exactly what they import.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
