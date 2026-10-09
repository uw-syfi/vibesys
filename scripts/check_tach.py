#!/usr/bin/env python3
"""Run `tach check` so that it enforces every library, workspace member or not.

Tach reads a `pyproject.toml` in a library directory as the root of a separate
package, and then stops checking imports into and out of that library against
`depends_on` and `[[interfaces]]` in `tach.toml`. A library that becomes a uv
workspace member therefore silently drops out of the architecture ratchet.

This wrapper copies the Python files of every `source_roots` entry, and
`tach.toml`, into a temporary tree that holds no `pyproject.toml`, and runs
`tach check` there. File paths in tach's output are relative to the source
roots, so they read the same as in the checkout. Extra arguments are passed to
`tach check`.

Usage:
    uv run python scripts/check_tach.py
    uv run python scripts/check_tach.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

EXIT_TOOL_ERROR = 2
_SKIPPED_DIRECTORIES = frozenset({"__pycache__", ".venv", "node_modules"})


def _keep_python_sources(directory: str, names: list[str]) -> set[str]:
    """Tell ``copytree`` to copy only Python files and the directories holding them."""
    base = Path(directory)
    return {
        name
        for name in names
        if name in _SKIPPED_DIRECTORIES or ((base / name).is_file() and not name.endswith(".py"))
    }


def stage_source_roots(repo_root: Path, destination: Path) -> None:
    """Copy `tach.toml` and every configured source root into ``destination``."""
    config_path = repo_root / "tach.toml"
    roots = tomllib.loads(config_path.read_text(encoding="utf-8"))["source_roots"]
    shutil.copy(config_path, destination / "tach.toml")
    for relative in roots:
        shutil.copytree(
            repo_root / relative, destination / relative, ignore=_keep_python_sources
        )


def main() -> int:
    """Run `tach check` on the staged copy and return its exit code."""
    parser = argparse.ArgumentParser(description="Run tach check with libraries unpackaged.")
    parser.add_argument("--root", type=Path, default=Path(), help="Repository root (default: cwd)")
    args, tach_args = parser.parse_known_args()
    repo_root = args.root.resolve()
    with tempfile.TemporaryDirectory(prefix="tach-check-") as scratch:
        staged = Path(scratch)
        try:
            stage_source_roots(repo_root, staged)
        except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
            print(f"check_tach: cannot stage the source roots: {error}", file=sys.stderr)
            return EXIT_TOOL_ERROR
        # lint-waiver: LW-936201 [S603]; fixed argument vector running this interpreter's tach
        # module in a directory this script created; no shell is involved.
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "tach", "check", *tach_args], cwd=staged, check=False
        ).returncode


if __name__ == "__main__":
    sys.exit(main())
