"""Stage bundled framework resources for inclusion in the Python wheel.

Profiler support packages (``resources/profilers``), evaluator packages
(``resources/evaluators``), and the preset skills library (``resources/skills``)
are resolved from the repository checkout at run time. An installed wheel has
no checkout, so the wheel build copies these
trees into the ``vibesys._resources`` package directory; ``setup.py`` calls
:func:`stage_resources` from the same custom ``build_py`` step that stages the
TUI. ``vibesys.resource_paths`` resolves the checkout first and falls back to
the staged copy.

Vendored skill repository checkouts (``repos/`` inside a skill) are excluded:
they are git submodules, are already excluded from workspace materialization,
and would bloat the wheel by hundreds of megabytes.

Like ``tui_packaging``, this module is imported inside an isolated build
environment, so it depends only on the standard library.
"""

from __future__ import annotations

import shutil
from pathlib import Path  # noqa: TC003  # tracked: #288

#: Subtrees of ``resources/`` staged into the wheel.
STAGED_TREES: tuple[str, ...] = ("evaluators", "profilers", "skills")

#: Directory names never copied, at any depth.
EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "repos",
        "target",
    }
)


class PackagingError(RuntimeError):
    """Raised when a required wheel input is absent or incomplete."""

    @classmethod
    def missing(cls, category: str, paths: list[Path]) -> PackagingError:
        """Build an error naming missing paths relative to the repository."""
        formatted = ", ".join(path.as_posix() for path in paths)
        return cls(f"Missing required {category}: {formatted}")


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names if name in EXCLUDED_NAMES or name.endswith((".pyc", ".egg-info"))
    }


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=_ignore)


def stage_resources(repo_root: Path, dest: Path, *, required: bool = False) -> bool:
    """Copy the staged resource trees into ``dest``; return whether staged.

    Returns ``False`` without side effects when the checkout has no
    ``resources/`` directory (e.g. a build from an incomplete source tree);
    the wheel then installs without bundled resources and run time falls back
    to requiring a checkout, exactly as before staging existed.
    """
    source_root = repo_root / "resources"
    sources = [(tree, source_root / tree) for tree in STAGED_TREES]
    missing = [source.relative_to(repo_root) for _tree, source in sources if not source.is_dir()]
    if required and missing:
        error = PackagingError.missing("resource trees", missing)
        raise error
    if not source_root.is_dir():
        return False
    if dest.exists():
        shutil.rmtree(dest)
    staged_any = False
    for tree, source in sources:
        if not source.is_dir():
            continue
        _copy_tree(source, dest / tree)
        staged_any = True
    return staged_any


def stage_sdk(repo_root: Path, dest: Path, *, required: bool = False) -> bool:
    """Stage the installable ``vs-bench`` source project into the wheel."""
    source = repo_root / "sdk" / "vs-bench"
    required_files = (source / "pyproject.toml", source / "README.md")
    required_directories = (source / "src",)
    missing = [entry.relative_to(repo_root) for entry in required_files if not entry.is_file()]
    missing.extend(
        entry.relative_to(repo_root) for entry in required_directories if not entry.is_dir()
    )
    if required and missing:
        error = PackagingError.missing("SDK inputs", missing)
        raise error
    if missing:
        return False

    if dest.exists():
        shutil.rmtree(dest)
    destination = dest / "vs-bench"
    destination.mkdir(parents=True)
    shutil.copy2(source / "pyproject.toml", destination / "pyproject.toml")
    shutil.copy2(source / "README.md", destination / "README.md")
    _copy_tree(source / "src", destination / "src")
    return True
