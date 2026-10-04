"""Helpers that define the Python packages owned by the VibeSys distribution."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PACKAGE_SOURCE_ROOTS = (
    Path("src"),
    Path("libs/vs-evaluator-protocol/src"),
    Path("libs/vs-evaluation/src"),
    Path("libs/vs-async-ops/src"),
    Path("libs/vs-github/src"),
    Path("libs/vs-issue-tracker/src"),
    Path("libs/vs-core/src"),
    Path("libs/vs-project/src"),
    Path("libs/vs-prompts/src"),
    Path("libs/vs-runtime/src"),
    Path("libs/vs-sandbox/src"),
    Path("libs/vs-agent/src"),
    Path("libs/vs-slurm/src"),
    Path("libs/vs-faults/src"),
)
_BUILD_AND_CACHE_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
    }
)
_CLIENT_LOCAL_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".browser-dist",
        ".vibesys-demo",
        "artifacts",
        "coverage",
        "dist",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)
_CLIENT_LOCAL_FILENAMES = frozenset({".env", ".envrc"})


class InvalidPackageNamesError(ValueError):
    """Raised when a distribution package has an invalid top-level name."""

    def __init__(self, package_names: set[str]) -> None:
        """Record the invalid package names for diagnostics."""
        self.package_names = package_names
        super().__init__("distribution package names must be Python identifiers")


class ClientSourceDiscoveryError(RuntimeError):
    """Raised when a checkout cannot identify its tracked client sources."""

    def __init__(self, repo_root: Path) -> None:
        """Name the checkout whose client source boundary could not be read."""
        super().__init__(f"could not read Git-tracked client sources from {repo_root}")


def release_has_native_payload() -> bool:
    """Return whether setuptools is building a target-specific release wheel."""
    return os.environ.get("VIBESYS_WHEEL_TARGET") is not None


def clear_distribution_build_outputs(build_lib: Path, packages: list[str]) -> None:
    """Remove stale build-tree copies of packages owned by this distribution."""
    top_level_packages = {package.partition(".")[0] for package in packages}
    if any(not package.isidentifier() for package in top_level_packages):
        raise InvalidPackageNamesError(top_level_packages)
    for package in top_level_packages:
        destination = build_lib / package
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.is_dir():
            shutil.rmtree(destination)


def discover_distribution_packages(repo_root: Path) -> tuple[list[str], dict[str, str]]:
    """Return every import package and its source directory for setuptools."""
    packages: list[str] = []
    package_dirs: dict[str, str] = {}

    for relative_root in PACKAGE_SOURCE_ROOTS:
        source_root = repo_root / relative_root
        discovered: list[str] = []
        for top_level_init in source_root.glob("*/__init__.py"):
            top_level = top_level_init.parent
            directories = (
                top_level,
                *sorted(path for path in top_level.rglob("*") if path.is_dir()),
            )
            for directory in directories:
                relative = directory.relative_to(source_root)
                if all(
                    part.isidentifier() and part not in _BUILD_AND_CACHE_DIRECTORIES
                    for part in relative.parts
                ):
                    discovered.append(".".join(relative.parts))
        packages.extend(discovered)
        package_dirs.update(
            {
                package: (relative_root / Path(*package.split("."))).as_posix()
                for package in discovered
            }
        )

    return sorted(packages), package_dirs


def discover_client_workspace_sources(repo_root: Path) -> list[str]:
    """Return tracked, canonical sources for the client workspace."""
    clients_root = repo_root / "clients"
    package_roots = sorted(
        manifest.parent
        for manifest in clients_root.glob("*/package.json")
        if manifest.parent.name not in {"bower_components", "node_modules"}
    )
    allowed_directories = {package_root.name for package_root in package_roots}
    if (clients_root / "scripts").is_dir():
        allowed_directories.add("scripts")

    tracked = _tracked_client_sources(repo_root)
    candidates = _walk_client_sources(clients_root, package_roots) if tracked is None else tracked
    return sorted(
        path.relative_to(repo_root).as_posix()
        for path in candidates
        if path.is_file()
        and not path.is_symlink()
        and _is_canonical_client_source(path, clients_root, allowed_directories)
    )


def _tracked_client_sources(repo_root: Path) -> tuple[Path, ...] | None:
    """Return Git-tracked client files, or ``None`` outside a checkout."""
    git_metadata = repo_root / ".git"
    git = shutil.which("git")
    if git is None:
        if git_metadata.exists():
            raise ClientSourceDiscoveryError(repo_root)
        return None
    try:
        # lint-waiver: LW-936101 [S603]; the resolved Git executable receives fixed
        # `-C`/`ls-files` arguments and the caller-provided repository path without a shell.
        result = subprocess.run(  # noqa: S603
            [git, "-C", str(repo_root), "ls-files", "-z", "--", "clients"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        if git_metadata.exists():
            raise ClientSourceDiscoveryError(repo_root) from error
        return None
    return tuple(
        repo_root / Path(os.fsdecode(value)) for value in result.stdout.split(b"\0") if value
    )


def _walk_client_sources(clients_root: Path, package_roots: list[Path]) -> tuple[Path, ...]:
    """Recover canonical sources when an unpacked sdist has no Git metadata."""
    roots = [clients_root, clients_root / "scripts", *package_roots]
    sources: list[Path] = []
    for source_root in roots:
        if not source_root.exists():
            continue
        if source_root == clients_root:
            sources.extend(path for path in source_root.iterdir() if path.is_file())
            continue
        for directory, child_directories, filenames in os.walk(source_root):
            child_directories[:] = sorted(
                child for child in child_directories if child not in _CLIENT_LOCAL_DIRECTORIES
            )
            directory_path = Path(directory)
            sources.extend(directory_path / filename for filename in sorted(filenames))
    return tuple(sources)


def _is_canonical_client_source(
    path: Path, clients_root: Path, allowed_directories: set[str]
) -> bool:
    try:
        relative = path.relative_to(clients_root)
    except ValueError:
        return False
    if not relative.parts:
        return False
    if len(relative.parts) > 1 and relative.parts[0] not in allowed_directories:
        return False
    if any(part in _CLIENT_LOCAL_DIRECTORIES for part in relative.parts):
        return False
    return relative.name not in _CLIENT_LOCAL_FILENAMES and not relative.name.startswith(".env.")


def is_client_workspace_path(path: str) -> bool:
    """Return whether a distribution path belongs to the client workspace."""
    parts = Path(path).parts
    return bool(parts and parts[0] == "clients")
