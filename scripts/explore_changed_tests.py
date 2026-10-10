#!/usr/bin/env python3
"""Pick the tests a pull request adds or changes, and rerun them across seeds and schedules.

The `seed-exploration` job in `.github/workflows/test.yml` runs this beside the test shards.
A green suite proves each test passes in one scheduling order under one seed; a test that
only passes in that order (a shared slot two tasks race for, a timer tie, a Fake that
answers in insertion order) merges green and fails later in somebody else's PR. This step
runs only what the PR touched, so the cost follows the size of the change:

* `select` (standard library only, so a PR that touches no tests pays for no environment
  setup) lists the test functions whose lines the diff adds or changes. A change outside any
  test function (a fixture, a helper, an import) selects the whole file.
* `run` hands the selection to pytest with the vs-sim plugin's exploration options: sim tests
  run under `--sim-explore` different seeds, each with its own schedule seed; other tests
  outside the real-system tiers run `--sim-repeat` times; real-system tiers are not run.
  A budget stops extra runs, never a test's first run. The elapsed time lands in the job
  summary.

Usage:
    python3 scripts/explore_changed_tests.py select --base origin/main --ids-file ids.txt
    uv run python scripts/explore_changed_tests.py run --ids-file ids.txt
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_SEEDS = 24
DEFAULT_REPEATS = 3
DEFAULT_BUDGET_SECONDS = 240.0
PYTEST_NO_TESTS_COLLECTED = 5

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class FunctionSpan:
    """One test function: its pytest node id (after the file) and the lines it occupies."""

    node: str
    first: int
    last: int


def is_test_file(path: str) -> bool:
    """Whether ``path`` is a pytest test module (conftest and support modules are not)."""
    name = PurePosixPath(path).name
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def changed_lines(diff: str) -> dict[str, list[tuple[int, int]]]:
    """The new-side line ranges each file's hunks cover, from ``git diff -U0`` output.

    A hunk that only deletes lines covers the line before and after the deletion.
    """
    result: dict[str, list[tuple[int, int]]] = {}
    for section in re.split(r"^diff --git ", diff, flags=re.MULTILINE)[1:]:
        file_match = _FILE.search(section)
        if file_match is None:
            continue  # a deletion or a pure mode change leaves no new side
        ranges = result.setdefault(file_match.group(1), [])
        for hunk in _HUNK.finditer(section):
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            ranges.append((start, start + count - 1) if count else (start, start + 1))
    return result


def function_spans(source: str) -> list[FunctionSpan]:
    """The test functions of a module: top-level ``test_*`` and methods of ``Test*`` classes."""
    spans: list[FunctionSpan] = []

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                spans.append(
                    FunctionSpan(f"{prefix}{node.name}", first, node.end_lineno or node.lineno)
                )
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(ast.parse(source).body, "")
    return spans


def select_ids(path: str, source: str, ranges: list[tuple[int, int]]) -> list[str]:
    """The pytest ids to run for one changed test file.

    Tests whose lines a hunk touches; the whole file when a hunk touches no test function.
    """
    spans = function_spans(source)
    chosen: dict[str, None] = {}
    for first, last in ranges:
        hit = [span for span in spans if span.first <= last and first <= span.last]
        if not hit:
            return [path]
        for span in hit:
            chosen[f"{path}::{span.node}"] = None
    return list(chosen)


def selected_ids(diff: str, read: Callable[[str], str]) -> list[str]:
    """Ids for every test file in ``diff``; ``read(path)`` returns a file's current source."""
    ids: list[str] = []
    for path, ranges in sorted(changed_lines(diff).items()):
        if is_test_file(path):
            ids.extend(select_ids(path, read(path), ranges))
    return ids


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        message = "git is not on PATH"
        raise SystemExit(message)
    # lint-waiver: LW-164001 [S603]; Git runs with fixed argv and no shell; the base ref is a workflow-supplied branch name. Reimplementing diff parsing would duplicate Git's own.
    return subprocess.run([git, *args], check=True, capture_output=True, text=True).stdout  # noqa: S603


def _select(args: argparse.Namespace) -> int:
    diff = _git(
        "diff", "-U0", "--no-color", "--diff-filter=AMR", f"{args.base}...HEAD", "--", "*.py"
    )
    ids = selected_ids(diff, lambda path: Path(path).read_text(encoding="utf-8"))
    Path(args.ids_file).write_text("".join(f"{i}\n" for i in ids), encoding="utf-8")
    print(f"{len(ids)} changed test selections")
    for line in ids:
        print(f"  {line}")
    _github_output("selected", "true" if ids else "false")
    return 0


def _github_output(key: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(f"{key}={value}\n")


def _run(args: argparse.Namespace) -> int:
    ids = [line for line in Path(args.ids_file).read_text(encoding="utf-8").split("\n") if line]
    command = [
        sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
        f"--sim-explore={args.seeds}", f"--sim-repeat={args.repeats}",
        f"--sim-explore-budget={args.budget}", *ids,
    ]  # fmt: skip
    started = time.monotonic()
    # lint-waiver: LW-164002 [S603]; The command is this interpreter running pytest with fixed options and ids this script wrote. There is no shell, and a wrapper would only forward the same argv.
    code = subprocess.run(command, check=False).returncode  # noqa: S603
    elapsed = time.monotonic() - started
    outcome = (
        "no explorable tests selected"
        if code == PYTEST_NO_TESTS_COLLECTED
        else ("passed" if code == 0 else "FAILED")
    )
    summary = (
        f"### Seed exploration\n\n{len(ids)} changed test selections, {args.seeds} seeds and "
        f"schedules per simulated test, {args.repeats} repeats per other test, budget "
        f"{args.budget:g} s.\n\nExploration took {elapsed:.1f} s: {outcome}.\n"
    )
    print(summary)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(summary)
    return 0 if code in {0, PYTEST_NO_TESTS_COLLECTED} else code


def main(argv: list[str] | None = None) -> int:
    """Run the ``select`` or ``run`` subcommand and return its exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="list the changed tests")
    select.add_argument("--base", required=True, help="the ref the pull request merges into")
    select.add_argument("--ids-file", required=True)
    run = commands.add_parser("run", help="explore the listed tests")
    run.add_argument("--ids-file", required=True)
    run.add_argument("--seeds", type=int, default=DEFAULT_SEEDS)
    run.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    run.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS)
    args = parser.parse_args(argv)
    return _select(args) if args.command == "select" else _run(args)


if __name__ == "__main__":
    sys.exit(main())
