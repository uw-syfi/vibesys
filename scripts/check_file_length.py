#!/usr/bin/env python3
"""Fail when a Python source file grows past the god-file ceiling.

Ruff already enforces the function-level quality rules this repo cares about
(`PL`, `C901` with `max-complexity = 10`), and Biome enforces the TypeScript
equivalents plus a per-file line ceiling. Neither tool measures Python *file*
length, so a module can grow without bound while every function in it stays
inside the limits. This script closes that gap.

The ceiling is a ratchet, not a hard cut: files already over it are recorded in
an explicit allowlist at their current length, so the check passes today and
fails the moment one of them grows. Shrinking a file is always allowed; the
script prints the entries worth tightening and refuses to let an entry that has
dropped back under the ceiling linger.

Configuration lives in `pyproject.toml` under `[tool.vibesys.file_length]`:

    max_lines  -- ceiling, in physical lines (what `wc -l` counts), for any
                  file that is not allowlisted.
    roots      -- repo-relative directories to scan for `*.py`. Any path with
                  a `tests` or `__pycache__` component is skipped: long test
                  modules are normal, and the same exemption applies to
                  TypeScript test files in `biome.json`.
    allowlist  -- table of repo-relative path -> recorded line count for files
                  currently over the ceiling. Every entry should be preceded
                  by a comment explaining why it is still there.

Three conditions fail:

    1. A non-allowlisted file exceeds `max_lines`.
    2. An allowlisted file exceeds its recorded count (the ratchet).
    3. An allowlist entry is stale: the file is gone, or it now fits under
       `max_lines` and the entry must be deleted.

Usage:
    uv run python scripts/check_file_length.py
    uv run python scripts/check_file_length.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PYPROJECT = Path("pyproject.toml")
SKIPPED_DIR_NAMES = frozenset({"tests", "__pycache__"})

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_TOOL_ERROR = 2


@dataclass(frozen=True)
class Config:
    """Resolved `[tool.vibesys.file_length]` settings."""

    max_lines: int
    roots: tuple[str, ...]
    allowlist: dict[str, int]


class ConfigError(Exception):
    """The `[tool.vibesys.file_length]` section is missing or malformed."""


def load_config(pyproject_path: Path) -> Config:
    """Read the file-length ceiling, scan roots, and allowlist from ``pyproject.toml``.

    Raises:
        ConfigError: If the section is absent or a required key is missing.
    """
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{pyproject_path}: cannot be read ({exc})") from exc  # noqa: TRY003  # tracked: #288
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{pyproject_path}: is not valid TOML ({exc})") from exc  # noqa: TRY003  # tracked: #288

    try:
        section = data["tool"]["vibesys"]["file_length"]
        max_lines = int(section["max_lines"])
        roots = tuple(str(entry) for entry in section["roots"])
    except KeyError as exc:
        raise ConfigError(  # noqa: TRY003  # tracked: #288
            f"{pyproject_path}: missing [tool.vibesys.file_length] key {exc}"
        ) from exc

    allowlist = {str(path): int(count) for path, count in section.get("allowlist", {}).items()}
    return Config(max_lines=max_lines, roots=roots, allowlist=allowlist)


def count_lines(path: Path) -> int:
    """Return the number of physical lines in ``path``, matching `wc -l`."""
    return len(path.read_text(encoding="utf-8").splitlines())


def measure(repo_root: Path, roots: tuple[str, ...]) -> dict[str, int]:
    """Map every scanned repo-relative `*.py` path to its physical line count."""
    measured: dict[str, int] = {}
    for root in roots:
        for path in sorted((repo_root / root).rglob("*.py")):
            relative = path.relative_to(repo_root)
            if SKIPPED_DIR_NAMES.intersection(relative.parts):
                continue
            measured[relative.as_posix()] = count_lines(path)
    return measured


def check_measured_files(measured: dict[str, int], config: Config) -> list[str]:
    """Report files over the ceiling that are not allowlisted, or over their recorded count."""
    failures: list[str] = []
    for path, lines in sorted(measured.items()):
        recorded = config.allowlist.get(path)
        if recorded is None:
            if lines > config.max_lines:
                failures.append(
                    f"  {path}: {lines} lines > {config.max_lines} (ceiling), and not allowlisted"
                )
        elif lines > recorded:
            failures.append(
                f"  {path}: {lines} lines > {recorded} (recorded); this file may only shrink"
            )
    return failures


def check_allowlist(measured: dict[str, int], config: Config) -> tuple[list[str], list[str]]:
    """Report stale allowlist entries, and entries whose recorded count can be tightened."""
    stale: list[str] = []
    tightenable: list[str] = []
    for path, recorded in sorted(config.allowlist.items()):
        lines = measured.get(path)
        if lines is None:
            stale.append(f"  {path}: no longer exists or is no longer scanned")
        elif lines <= config.max_lines:
            stale.append(f"  {path}: now {lines} lines, at or under the {config.max_lines} ceiling")
        elif lines < recorded:
            tightenable.append(f"  {path}: {recorded} -> {lines}")
    return stale, tightenable


def report(failures: list[str], stale: list[str], tightenable: list[str], ceiling: int) -> int:
    """Print the outcome and return the process exit code."""
    if failures:
        print(f"Python files over the {ceiling}-line ceiling or over their recorded length:")
        for line in failures:
            print(line)
        print(
            "\nSplit the module, or (for a deliberate, reviewed exception) record it in "
            "[tool.vibesys.file_length.allowlist] in pyproject.toml with a comment."
        )
    if stale:
        if failures:
            print()
        print("Stale [tool.vibesys.file_length.allowlist] entries; delete them:")
        for line in stale:
            print(line)
    if failures or stale:
        return EXIT_VIOLATIONS

    print(f"All scanned Python files are within the {ceiling}-line ceiling or their recorded size.")
    if tightenable:
        print("\nAllowlist entries that shrank; lower the recorded count to lock the gain in:")
        for line in tightenable:
            print(line)
    return EXIT_OK


def main() -> int:
    """Enforce the Python file-length ratchet, returning a process exit code."""
    parser = argparse.ArgumentParser(description="Enforce the Python file-length ratchet.")
    parser.add_argument(
        "--root", type=Path, default=Path(), help="Repository root to scan (default: cwd)"
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=None,
        help="pyproject.toml holding the configuration (default: <root>/pyproject.toml)",
    )
    args = parser.parse_args()
    pyproject_path = args.pyproject if args.pyproject is not None else args.root / DEFAULT_PYPROJECT

    try:
        config = load_config(pyproject_path)
        measured = measure(args.root, config.roots)
    except (ConfigError, OSError) as exc:
        print(f"check_file_length: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    failures = check_measured_files(measured, config)
    stale, tightenable = check_allowlist(measured, config)
    return report(failures, stale, tightenable, config.max_lines)


if __name__ == "__main__":
    raise SystemExit(main())
