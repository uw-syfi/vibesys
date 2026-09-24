#!/usr/bin/env python3
"""Fail when any measured module's coverage falls below a per-module floor.

`coverage`/`pytest-cov` only expose an aggregate `--cov-fail-under`, so a
module sitting at or near zero coverage can hide behind a healthy repo-wide
average. This script re-reads a `coverage json` report and enforces a floor
per source file, with an explicit, commented allowlist for modules that are
intentionally below it.
Configuration lives in `pyproject.toml` under
`[tool.vibesys.per_module_coverage]`:
    floor      -- minimum percent-covered (statement + branch, combined by
                  `coverage`) for any non-allowlisted file.
    allowlist  -- repo-relative file paths, or directory prefixes ending in
                  "/", permitted to fall below the floor. Every entry must
                  be preceded by a comment explaining why it is there.
Usage:
    uv run python scripts/check_coverage_floor.py coverage.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

DEFAULT_PYPROJECT = Path("pyproject.toml")


def load_config(pyproject_path: Path) -> tuple[float, list[str]]:
    """Read the per-module coverage floor and allowlist from ``pyproject.toml``.

    Raises:
        SystemExit: If the ``[tool.vibesys.per_module_coverage]`` section is absent.
    """
    data = tomllib.loads(pyproject_path.read_text())
    try:
        section = data["tool"]["vibesys"]["per_module_coverage"]
    except KeyError:
        sys.exit(f"{pyproject_path}: missing [tool.vibesys.per_module_coverage] section")
    floor = float(section["floor"])
    allowlist = list(section.get("allowlist", []))
    return floor, allowlist


def is_allowlisted(path: str, allowlist: list[str]) -> bool:
    """Return whether ``path`` is exempt, matching allowlist entries exactly or by directory prefix."""
    for entry in allowlist:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def main() -> int:
    """Enforce the per-module coverage floor, returning a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json", type=Path, help="Path to a `coverage json` report")
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    args = parser.parse_args()
    floor, allowlist = load_config(args.pyproject)
    report = json.loads(args.coverage_json.read_text())
    failures: list[tuple[str, float]] = []
    seen_exact_allowlist_entries: set[str] = set()
    for path, file_report in sorted(report["files"].items()):
        summary = file_report["summary"]
        if summary["num_statements"] == 0:
            # Nothing executable to measure, e.g. an empty __init__.py.
            continue
        percent = summary["percent_covered"]
        allowlisted = is_allowlisted(path, allowlist)
        if allowlisted and path in allowlist:
            seen_exact_allowlist_entries.add(path)
        if percent < floor and not allowlisted:
            failures.append((path, percent))
    # Directory-prefix entries ("foo/") can't go stale the same way an exact
    # path can, so only flag exact-path entries that named a file the report
    # no longer contains (renamed, deleted, or a typo).
    stale = [
        entry
        for entry in allowlist
        if not entry.endswith("/") and entry not in seen_exact_allowlist_entries
    ]
    exit_code = 0
    if failures:
        exit_code = 1
        print(f"Per-module coverage floor ({floor:.0f}%) not met:", file=sys.stderr)
        for path, percent in failures:
            print(f"  {path}: {percent:.1f}% < {floor:.0f}%", file=sys.stderr)
        print(
            "\nEither add tests to raise coverage, or add the module to the "
            "explicit, commented allowlist under "
            "[tool.vibesys.per_module_coverage] in pyproject.toml.",
            file=sys.stderr,
        )
    if stale:
        exit_code = 1
        print(
            "Stale [tool.vibesys.per_module_coverage] allowlist entries "
            "(no matching file in the coverage report):",
            file=sys.stderr,
        )
        for entry in stale:
            print(f"  {entry}", file=sys.stderr)
    if exit_code == 0:
        print(f"All measured modules meet the {floor:.0f}% per-module coverage floor.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
