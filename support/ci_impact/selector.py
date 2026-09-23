"""Traverse the CI impact graph and emit an explained plan."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from .model import JOBS, ROOT, Component, _fail

if TYPE_CHECKING:
    from pathlib import Path


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _owners(path: str, components: dict[str, Component]) -> list[str]:
    owners = [
        name
        for name, component in components.items()
        if path in component.files or any(_under(path, root) for root in component.roots)
    ]
    for prefix in ("python:", "pnpm:", "native:"):
        family = [name for name in owners if name.startswith(prefix)]
        if family:
            deepest = max(len(components[name].roots[0]) for name in family)
            owners = [
                name
                for name in owners
                if name not in family or len(components[name].roots[0]) == deepest
            ]
    return owners


def _direct_reasons(
    paths: list[str],
    components: dict[str, Component],
    ignored_roots: tuple[str, ...],
    ignored_files: tuple[str, ...],
) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}
    unknown: list[str] = []
    for path in paths:
        owners = _owners(path, components)
        if not owners:
            if path in ignored_files or any(_under(path, root) for root in ignored_roots):
                continue
            unknown.append(path)
            continue
        for name in owners:
            reasons.setdefault(name, []).append(f"changed {path}")
    if unknown:
        _fail(f"unowned changed paths: {', '.join(sorted(unknown))}")
    return reasons


def _native_languages(targets: list[str], root: Path = ROOT) -> list[str]:
    languages = set()
    for target in targets:
        directory = root / target
        if (directory / "Cargo.toml").is_file():
            languages.add("rust")
        elif (directory / "go.mod").is_file():
            languages.add("go")
        else:
            _fail(f"native target {target!r} has no Cargo.toml or go.mod")
    return sorted(languages)


def select(
    paths: list[str],
    components: dict[str, Component],
    ignored_roots: tuple[str, ...],
    ignored_files: tuple[str, ...],
) -> dict[str, Any]:
    """Traverse reverse dependencies from changed path owners."""
    reasons = _direct_reasons(paths, components, ignored_roots, ignored_files)
    dependents: dict[str, list[str]] = {name: [] for name in components}
    for name, component in components.items():
        for dependency in component.dependencies:
            dependents[dependency].append(name)
    queue = deque(reasons)
    while queue:
        source = queue.popleft()
        for dependent in dependents[source]:
            if dependent in reasons:
                continue
            reasons[dependent] = [f"depends on {source}: {reasons[source][0]}"]
            queue.append(dependent)
    jobs = dict.fromkeys(JOBS, False)
    job_reasons: dict[str, list[str]] = {job: [] for job in JOBS}
    for name, why in reasons.items():
        for job in components[name].jobs:
            jobs[job] = True
            job_reasons[job].append(f"{name}: {why[0]}")
    native_targets = sorted(
        name.removeprefix("native:")
        for name in reasons
        if name.startswith("native:") and components[name].jobs
    )
    if "ci_policy" in reasons:
        native_targets = sorted(
            name.removeprefix("native:")
            for name, component in components.items()
            if name.startswith("native:") and component.jobs
        )
    pnpm_packages = sorted(
        name.removeprefix("pnpm:") for name in reasons if name.startswith("pnpm:")
    )
    return {
        "jobs": jobs,
        "components": reasons,
        "job_reasons": job_reasons,
        "changed_paths": paths,
        "native_targets": native_targets,
        "native_languages": _native_languages(native_targets),
        "pnpm_packages": pnpm_packages,
    }
