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
  The step has a hard wall-clock cap, so it ends before the test shards and coverage
  whatever the size of the PR: pytest stops starting runs `TAIL_SECONDS` before the cap,
  and at the cap this script interrupts it, which ends exploration with the results so
  far. The cap may cut a test's first exploration run too, since the shards already run
  every changed test once. Exploration runs in rounds (every test once, then every test
  again), and the PR's head commit salts the seeds and the test each round starts from, so
  a large PR's pushes still cover all its tests over time. The job summary lists what ran
  and what the cap left unexplored. With ``--failures-file``, the (test, seed) pairs that
  failed on the previous push run first, and the file is rewritten with this push's
  failures: a failure found under a salted seed is retried until it passes, not lost
  when the next push changes the salt. CI keeps the file in a per-PR cache entry.

Usage:
    python3 scripts/explore_changed_tests.py select --base origin/main --ids-file ids.txt
    uv run python scripts/explore_changed_tests.py run --ids-file ids.txt --salt "$HEAD_SHA" \
        --failures-file failures.jsonl
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_SEEDS = 24
DEFAULT_REPEATS = 3
# The cap keeps the job (about 25 s of checkout and environment setup plus this step) at
# least 30 s ahead of the critical path: the test shards then coverage, about 4 to 4.5
# minutes after the run starts, against this job's start at about 0.5 minutes.
DEFAULT_CAP_SECONDS = 150.0
# Pytest starts no run this close to the cap, so the interrupt at the cap normally finds it
# finished; an interrupted pytest gets `GRACE_SECONDS` to report before it is killed.
TAIL_SECONDS = 20.0
GRACE_SECONDS = 10.0
PYTEST_NO_TESTS_COLLECTED = 5
LISTED_UNEXPLORED = 50
# Above this share of explorable tests left unexplored, the step warns on the PR: a green
# step then says little about the changed tests' other seeds.
UNEXPLORED_WARNING_SHARE = 0.5

REPO_ROOT = Path(__file__).resolve().parents[1]

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


def suite_pathspecs(pyproject_text: str) -> list[str]:
    """Git pathspecs for the Python files under the suite's ``testpaths``.

    Only tests the main suite collects are explored. Example projects carry their own
    test suites with their own runners and import roots (``cpu_check/run.sh``), so
    collecting them from the repository root would fail for reasons the PR did not cause.
    """
    roots = tomllib.loads(pyproject_text)["tool"]["pytest"]["ini_options"]["testpaths"]
    return [f":(glob){root}/**/*.py" for root in roots]


def _select(args: argparse.Namespace) -> int:
    pathspecs = suite_pathspecs((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    diff = _git(
        "diff", "-U0", "--no-color", "--diff-filter=AMR", f"{args.base}...HEAD", "--", *pathspecs
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


@dataclass(frozen=True)
class Explored:
    """One selected test's exploration, as the vs-sim plugin's ``--sim-explore-report`` writes it."""

    test: str
    runs: int
    planned: int
    outcome: str
    seed: int | None = None
    """The seed of a sim test's failing run."""


def read_report(text: str) -> list[Explored]:
    """The rows of an exploration report (JSON lines)."""
    return [Explored(**json.loads(line)) for line in text.splitlines() if line.strip()]


def failures(rows: list[Explored]) -> str:
    """The replay file for the next push: each failing sim test with its failing seed."""
    return "".join(
        json.dumps({"test": row.test, "seed": row.seed}) + "\n"
        for row in rows
        if row.outcome == "failed" and row.seed is not None
    )


def summary(
    rows: list[Explored], *, selections: int, cap: float, elapsed: float, verdict: str
) -> str:
    """The job summary: what exploration ran, and what the cap left unexplored."""
    unexplored = [row.test for row in rows if row.outcome == "not explored"]
    explored = len(rows) - len(unexplored)
    lines = [
        "### Seed exploration",
        "",
        f"{selections} changed test selections; {len(rows)} explorable tests, {explored} explored."
        f" {sum(row.runs for row in rows)} of {sum(row.planned for row in rows)} planned runs"
        f" in {elapsed:.1f} s: {verdict}.",
    ]
    failed = [row.test for row in rows if row.outcome == "failed"]
    if failed:
        lines += ["", "Failed (the log prints the seed that replays each):"]
        lines += [f"- `{test}`" for test in failed]
    if unexplored:
        lines += [
            "",
            f"The {cap:g} s cap left {len(unexplored)} tests unexplored this push"
            " (the next push's commit starts the rounds elsewhere; the shards ran each once):",
        ]
        lines += [f"- `{test}`" for test in unexplored[:LISTED_UNEXPLORED]]
        if len(unexplored) > LISTED_UNEXPLORED:
            lines.append(f"- and {len(unexplored) - LISTED_UNEXPLORED} more")
    return "\n".join(lines) + "\n"


def unexplored_warning(rows: list[Explored], *, cap: float) -> str | None:
    """A GitHub warning annotation when the cap left too many tests unexplored, else None."""
    unexplored = sum(1 for row in rows if row.outcome == "not explored")
    if not rows or unexplored / len(rows) <= UNEXPLORED_WARNING_SHARE:
        return None
    return (
        f"::warning title=Seed exploration cut short::the {cap:g} s cap left {unexplored} of"
        f" {len(rows)} changed tests unexplored; the job summary lists them"
    )


def outcome(code: int, *, capped: bool) -> tuple[bool, str]:
    """Whether the step fails, and the verdict for the summary, from pytest's exit code.

    Only a test failure (exit code 1) fails a capped run: the interrupt at the cap makes
    pytest exit 2 when it lands outside a test run (during collection), or the process dies
    to the kill after the grace period, and neither is the PR's fault.
    """
    if code == 0 or (capped and code != 1):
        return False, "passed, stopped at the cap" if capped else "passed"
    if code == PYTEST_NO_TESTS_COLLECTED:
        return False, "no explorable tests selected"
    return True, "FAILED"


def _pytest_within(command: list[str], cap: float) -> tuple[int, bool]:
    """Run pytest; interrupt it at ``cap`` seconds. Returns its exit code and whether the cap hit."""
    # lint-waiver: LW-164002 [S603]; The command is this interpreter running pytest with fixed options and ids this script wrote. There is no shell, and a wrapper would only forward the same argv.
    process = subprocess.Popen(command)  # noqa: S603
    try:
        return process.wait(timeout=cap), False
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=GRACE_SECONDS), True
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(), True


def _run(args: argparse.Namespace) -> int:
    ids = [line for line in Path(args.ids_file).read_text(encoding="utf-8").split("\n") if line]
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "explored.jsonl"
        command = [
            sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
            f"--sim-explore={args.seeds}", f"--sim-repeat={args.repeats}",
            f"--sim-explore-budget={max(args.cap - TAIL_SECONDS, 0.0):g}",
            f"--sim-explore-salt={args.salt}", f"--sim-explore-report={report}",
            *([f"--sim-explore-replay={args.failures_file}"] if args.failures_file else []), *ids,
        ]  # fmt: skip
        started = time.monotonic()
        code, capped = _pytest_within(command, args.cap)
        elapsed = time.monotonic() - started
        reported = report.exists()
        rows = read_report(report.read_text(encoding="utf-8")) if reported else []
    # Without a report (pytest stopped before writing one) the earlier failures stay listed.
    if args.failures_file and reported:
        Path(args.failures_file).write_text(failures(rows), encoding="utf-8")
    failed, verdict = outcome(code, capped=capped)
    text = summary(rows, selections=len(ids), cap=args.cap, elapsed=elapsed, verdict=verdict)
    print(text)
    warning = unexplored_warning(rows, cap=args.cap)
    if warning is not None:
        print(warning)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(text)
    return code if failed else 0


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
    run.add_argument("--cap", type=float, default=DEFAULT_CAP_SECONDS, help="wall seconds")
    run.add_argument("--salt", default="", help="varies seeds and order (CI: the head commit)")
    run.add_argument(
        "--failures-file",
        default=None,
        help="replay the failures listed here first, then rewrite it with this run's",
    )
    args = parser.parse_args(argv)
    return _select(args) if args.command == "select" else _run(args)


if __name__ == "__main__":
    sys.exit(main())
