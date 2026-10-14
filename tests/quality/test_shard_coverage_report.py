"""CI's test shards write coverage data and leave every report to the combine job.

A shard's report is thrown away: the `coverage` job combines the shards' data
and reports once. Building a term report on a shard costs 15 to 25 s of the
critical path (coverage parses every measured source file), so the shard
command's empty ``--cov-report=`` must leave pytest-cov with no report at all,
after pytest prepends ``addopts`` from pyproject.toml.

These tests read the real configuration and parse it with pytest-cov's own
``--cov-report`` action, so they follow pytest-cov's merge rule rather than a
copy of it.
"""

from __future__ import annotations

import argparse
import shlex
import tomllib
from pathlib import Path

from pytest_cov.plugin import StoreReport, validate_report

REPO = Path(__file__).resolve().parents[2]
TERMINAL_REPORTS = {"term", "term-missing"}


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _addopts() -> list[str]:
    return shlex.split(_pyproject()["tool"]["pytest"]["ini_options"]["addopts"])


def _shard_command() -> list[str]:
    checks = tomllib.loads((REPO / ".repoctl/checks.toml").read_text(encoding="utf-8"))
    (group,) = [g for g in checks["check_groups"] if g["name"] == "python_ci_tests_shard"]
    (command,) = group["commands"]
    return command[command.index("pytest") + 1 :]


def _reports(args: list[str]) -> dict[str, str | None]:
    """The reports pytest-cov builds for these arguments, by its own option action."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--cov-report", action=StoreReport, default={}, type=validate_report)
    namespace, _ = parser.parse_known_args(args)
    # pytest-cov falls back to a terminal report only when no --cov-report was given.
    return namespace.cov_report or {"term": None}


def test_a_ci_shard_builds_no_coverage_report() -> None:
    reports = _reports(_addopts() + _shard_command())
    assert set(reports) == {""}, (
        f"a CI shard builds coverage reports {sorted(set(reports) - {''})}: its "
        "`--cov-report=` cannot remove a report that pyproject.toml addopts names"
    )


def test_a_bare_local_run_still_shows_missing_lines() -> None:
    reports = _reports(_addopts())
    assert set(reports) & TERMINAL_REPORTS
    assert _pyproject()["tool"]["coverage"]["report"]["show_missing"] is True
