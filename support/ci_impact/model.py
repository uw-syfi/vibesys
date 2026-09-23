"""Policy validation and language-manifest component discovery."""

from __future__ import annotations

import graphlib
import json
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Never

JOBS = ("python", "tui", "examples", "agent_image", "evaluators", "go_prototype")
ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git") or "/usr/bin/git"


class SelectionError(ValueError):
    """An invalid diff or component policy cannot be selected safely."""


def _fail(message: str) -> Never:
    raise SelectionError(message)


@dataclass(frozen=True)
class Component:
    """Named source owner and its CI dependencies."""

    id: str
    roots: tuple[str, ...]
    files: tuple[str, ...]
    dependencies: tuple[str, ...]
    jobs: tuple[str, ...]
    adapter: str | None


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        _fail(f"{label} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        _fail(f"{label} has duplicate entries")
    return tuple(value)


def _safe_path(value: str, label: str) -> str:
    path = Path(value)
    if path.is_absolute() or not value or any(part in (".", "..") for part in path.parts):
        _fail(f"{label} has unsafe path {value!r}")
    return value.rstrip("/")


def load_policy(
    path: Path = ROOT / "ci-components.toml",
) -> tuple[dict[str, Component], tuple[str, ...], tuple[str, ...]]:
    """Load and validate the repository component policy."""
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    expected_keys = {
        "components",
        "edges",
        "ignored_roots",
        "ignored_files",
        "native_ci_roots",
        "native_ci_scope_roots",
    }
    if set(data) != expected_keys:
        _fail(
            f"{path}: missing keys {sorted(expected_keys - data.keys())}; "
            f"unexpected keys {sorted(data.keys() - expected_keys)}"
        )
    ignored_roots = tuple(
        _safe_path(x, "ignored_roots") for x in _strings(data["ignored_roots"], "ignored_roots")
    )
    ignored_files = tuple(
        _safe_path(x, "ignored_files") for x in _strings(data["ignored_files"], "ignored_files")
    )
    if not isinstance(data["components"], list) or not data["components"]:
        _fail(f"{path}: components must be a nonempty list")
    components = {}
    for entry in data["components"]:
        component = _parse_component(entry)
        if component.id in components:
            _fail(f"duplicate component id {component.id!r}")
        components[component.id] = component
    native_ci_roots = tuple(
        _safe_path(x, "native_ci_roots")
        for x in _strings(data["native_ci_roots"], "native_ci_roots")
    )
    native_ci_scope_roots = tuple(
        _safe_path(x, "native_ci_scope_roots")
        for x in _strings(data["native_ci_scope_roots"], "native_ci_scope_roots")
    )
    _add_native_components(components, native_ci_roots, native_ci_scope_roots)
    _apply_edges(components, data["edges"])
    _validate_native_targets(components, native_ci_roots, native_ci_scope_roots)
    try:
        tuple(
            graphlib.TopologicalSorter(
                {name: component.dependencies for name, component in components.items()}
            ).static_order()
        )
    except graphlib.CycleError as error:
        _fail(f"component dependency cycle: {error}")
    return components, ignored_roots, ignored_files


def _parse_component(entry: object) -> Component:
    if not isinstance(entry, dict) or set(entry) - {
        "id",
        "roots",
        "files",
        "depends_on",
        "job",
        "jobs",
        "root",
    }:
        _fail(f"invalid component entry {entry!r}")
    name = entry.get("id")
    if not isinstance(name, str) or not name:
        _fail(f"invalid component id {name!r}")
    roots = _strings(entry.get("roots", []), f"{name}.roots")
    files = _strings(entry.get("files", []), f"{name}.files")
    if "root" in entry:
        if not isinstance(entry["root"], str):
            _fail(f"{name}.root must be a path")
        roots += (entry["root"],)
    roots = tuple(_safe_path(x, f"{name}.roots") for x in roots)
    files = tuple(_safe_path(x, f"{name}.files") for x in files)
    deps = _strings(entry.get("depends_on", []), f"{name}.depends_on")
    jobs = _strings(entry.get("jobs", []), f"{name}.jobs")
    if "job" in entry:
        jobs += (entry["job"],)
    if any(job not in JOBS for job in jobs):
        _fail(f"{name}: unknown job in {jobs!r}")
    return Component(name, roots, files, deps, jobs, None)


def _apply_edges(components: dict[str, Component], edges: object) -> None:
    if not isinstance(edges, list):
        _fail("edges must be a list")
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"from", "to"}:
            _fail("edge must have from and to")
        source, target = edge["from"], edge["to"]
        if not isinstance(source, str) or not isinstance(target, str):
            _fail(f"edge endpoints must be component ids: {edge!r}")
        if source not in components or target not in components:
            _fail(f"unknown edge {source!r} -> {target!r}")
        components[target] = replace(
            components[target], dependencies=(*components[target].dependencies, source)
        )
    for name, component in components.items():
        for dependency in component.dependencies:
            if dependency not in components:
                _fail(f"{name}: unknown dependency {dependency!r}")
            if dependency == name:
                _fail(f"{name}: self dependency")


def _add_native_components(
    components: dict[str, Component],
    native_ci_roots: tuple[str, ...],
    native_ci_scope_roots: tuple[str, ...],
) -> None:
    """Derive file owners from language manifests, never a mirrored CI path list."""
    with (ROOT / "tach.toml").open("rb") as stream:
        tach = tomllib.load(stream)
    for module in tach["modules"]:
        name = module["path"]
        module_path = Path(*name.split("."))
        matches = [
            root / module_path
            for root in map(Path, tach["source_roots"])
            if (ROOT / root / module_path).is_dir()
            or (ROOT / root / module_path).with_suffix(".py").is_file()
        ]
        if len(matches) != 1:
            _fail(f"Tach module {name!r} has {len(matches)} source directories")
        candidate = matches[0]
        relative = (
            candidate.as_posix()
            if (ROOT / candidate).is_dir()
            else candidate.with_suffix(".py").as_posix()
        )
        ident = f"python:{name}"
        dependencies = tuple(f"python:{dep}" for dep in module["depends_on"])
        components[ident] = Component(ident, (relative,), (), dependencies, ("python",), None)
    package_files = sorted((ROOT / "clients").glob("*/package.json"))
    package_names = {}
    for package_file in package_files:
        package = json.loads(package_file.read_text(encoding="utf-8"))
        name = package.get("name")
        if not isinstance(name, str) or not name:
            _fail(f"{package_file}: missing package name")
        ident = f"pnpm:{name}"
        components[ident] = Component(
            ident, (package_file.parent.relative_to(ROOT).as_posix(),), (), (), ("tui",), None
        )
        package_names[name] = package
    for name, package in package_names.items():
        dependencies = tuple(
            f"pnpm:{dep}"
            for key in ("dependencies", "devDependencies", "optionalDependencies")
            for dep, version in package.get(key, {}).items()
            if isinstance(version, str) and version.startswith("workspace:")
        )
        ident = f"pnpm:{name}"
        components[ident] = replace(components[ident], dependencies=dependencies)
    for manifest_name in ("Cargo.toml", "go.mod"):
        listing = subprocess.run(  # noqa: S603
            [GIT, "ls-files", "--cached", "--others", "--exclude-standard", f"**/{manifest_name}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
        for relative_manifest in listing.stdout.splitlines():
            manifest = ROOT / relative_manifest
            relative = manifest.parent.relative_to(ROOT).as_posix()
            ident = f"native:{relative}"
            jobs = _native_jobs(relative, native_ci_roots, native_ci_scope_roots)
            components[ident] = Component(ident, (relative,), (), (), jobs, None)
    for root in native_ci_roots:
        if not any(
            component.roots and component.roots[0] == root and component.id.startswith("native:")
            for component in components.values()
        ):
            _fail(f"native_ci_roots: no manifest at {root}")


def _native_jobs(
    root: str, native_ci_roots: tuple[str, ...], native_ci_scope_roots: tuple[str, ...]
) -> tuple[str, ...]:
    if root in native_ci_roots:
        return ("evaluators",)
    if any(root == scope or root.startswith(scope + "/") for scope in native_ci_scope_roots):
        _fail(f"native evaluator {root!r} has no registered CI target")
    return ()


def _validate_native_targets(
    components: dict[str, Component],
    native_ci_roots: tuple[str, ...],
    native_ci_scope_roots: tuple[str, ...],
) -> None:
    """Every native manifest in a checked area must reach a runnable target."""
    dependents: dict[str, list[str]] = {name: [] for name in components}
    for name, component in components.items():
        for dependency in component.dependencies:
            dependents[dependency].append(name)
    checked_roots = (*native_ci_roots, *native_ci_scope_roots)
    for name, component in components.items():
        if not name.startswith("native:"):
            continue
        root = component.roots[0]
        if not any(root == area or root.startswith(area + "/") for area in checked_roots):
            continue
        pending = [name]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(dependents[current])
        if not any("evaluators" in components[current].jobs for current in seen):
            _fail(f"native manifest {root!r} does not reach a registered CI target")
