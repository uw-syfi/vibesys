#!/usr/bin/env python3
"""Run the Python tests a change can reach through imports, within a time budget.

Two callers want a fast, honest subset of the suite: the pre-push gate (`repoctl test --gate`)
and the pull-request commit stage of CI. The full suite still runs in the merge queue before
every merge, so this selection only has to be quick and to follow the change.

A test file is impacted when it imports, directly or through other first-party modules, a
module the change touches. A touched test file is impacted at distance 0, a test importing a
touched module at distance 1, and so on. A `conftest.py` is imported by every test below it.
Files are taken closest first, cheapest first (recorded seconds, `tests/support/
shard_durations.json`), until the time budget is spent. Touched test files always run.
What the budget leaves out, and any non-Python input a test reads, is left to the merge
queue; the report says how many tests that is.

The real-system tiers (`tests/e2e`, `tests/slurm_cluster`, `tests/minimal_container`) are never
selected: they need Docker, a cluster or credentials and have their own CI jobs.

Usage:
    uv run python -m scripts.impacted_tests select --base-ref origin/main
    uv run python -m scripts.impacted_tests run --base-ref origin/main --budget-seconds 30 --workers 4
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DURATIONS = REPO_ROOT / "tests" / "support" / "shard_durations.json"
#: Directories of real-system tests. Equal to the vs-sim plugin's `DEFAULT_REAL_TIERS`
#: (`test_the_real_tiers_match_the_pytest_plugin`).
REAL_TIERS = ("tests/e2e", "tests/slurm_cluster", "tests/minimal_container")
#: Module roots outside any `src` directory, as `[tool.pytest.ini_options] pythonpath` lists them
#: (the repository root is implicit).
EXTRA_ROOTS = ("packaging", "libs/vs-sim/pytest_plugin")
#: Seconds assumed for a test file the record does not know, so a new file is never free.
UNKNOWN_FILE_SECONDS = 2.5


@dataclass(frozen=True)
class Budget:
    """Wall seconds the selected tests may take on `workers` parallel workers."""

    seconds: float
    workers: int

    def wall(self, cpu_seconds: float) -> float:
        """Wall seconds `cpu_seconds` of recorded test time takes on the workers."""
        return cpu_seconds / self.workers


@dataclass(frozen=True)
class Selection:
    """The test files to run, and what the budget left for the merge queue."""

    files: tuple[str, ...]
    left_out: tuple[str, ...]
    seconds: float


def module_name(path: str) -> str:
    """The dotted name Python imports `path` under; `a/b/__init__.py` is `a.b`."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if "src" in parts:
        parts = parts[len(parts) - parts[::-1].index("src") :]
    else:
        for root in EXTRA_ROOTS:
            prefix = list(PurePosixPath(root).parts)
            if parts[: len(prefix)] == prefix:
                parts = parts[len(prefix) :]
                break
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def is_test_file(path: str, testpaths: Sequence[str]) -> bool:
    """True for a `test_*.py` file the main suite collects outside the real-system tiers."""
    posix = PurePosixPath(path)
    if posix.suffix != ".py" or not posix.name.startswith("test_"):
        return False
    if any(_under(path, tier) for tier in REAL_TIERS):
        return False
    return any(_under(path, root) for root in testpaths)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def imported_names(path: str, source: str) -> set[str]:
    """Every absolute module name `source` imports, with each ancestor package."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    package = module_name(path).split(".")
    if PurePosixPath(path).name != "__init__.py":
        package = package[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            if base is None:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    ancestors: set[str] = set()
    for name in names:
        parts = name.split(".")
        ancestors.update(".".join(parts[:end]) for end in range(1, len(parts)))
    return {name for name in names | ancestors if name}


def _from_base(node: ast.ImportFrom, package: list[str]) -> str | None:
    if node.level == 0:
        return node.module or ""
    keep = len(package) - (node.level - 1)
    if keep < 0:
        return None
    return ".".join([*package[:keep], *([node.module] if node.module else [])])


def importers(sources: Mapping[str, str], testpaths: Sequence[str]) -> dict[str, set[str]]:
    """Map a module name to the names of the modules that import it.

    A name need not belong to a file that exists: the tests that import a deleted module are
    still reached from it. A `conftest.py` counts as imported by every test file at or below its directory.
    """
    reverse: defaultdict[str, set[str]] = defaultdict(set)
    tests = [path for path in sources if is_test_file(path, testpaths)]
    for path, source in sources.items():
        own = module_name(path)
        for name in imported_names(path, source):
            if name != own:
                reverse[name].add(own)
    for path in sources:
        if PurePosixPath(path).name != "conftest.py":
            continue
        folder = str(PurePosixPath(path).parent)
        for test in tests:
            if folder == "." or _under(test, folder):
                reverse[module_name(path)].add(module_name(test))
    return dict(reverse)


def impacted(changed: Iterable[str], reverse: Mapping[str, set[str]]) -> dict[str, int]:
    """Distance from the changed modules to each module that can reach one by imports."""
    distance = {module_name(path): 0 for path in changed if path.endswith(".py")}
    queue = deque(distance)
    while queue:
        name = queue.popleft()
        for importer in sorted(reverse.get(name, ())):
            if importer not in distance:
                distance[importer] = distance[name] + 1
                queue.append(importer)
    return distance


def select(
    changed: Sequence[str],
    sources: Mapping[str, str],
    durations: Mapping[str, float],
    *,
    testpaths: Sequence[str],
    budget: Budget,
) -> Selection:
    """The impacted test files that fit the budget, closest to the change first."""
    reverse = importers(sources, testpaths)
    reached = impacted(changed, reverse)
    unknown = sum(durations.values()) / len(durations) if durations else UNKNOWN_FILE_SECONDS
    touched = {path for path in changed if is_test_file(path, testpaths) and path in sources}

    def cost(path: str) -> float:
        return durations.get(path, unknown)

    candidates = sorted(
        (
            (reached[module_name(path)], cost(path), path)
            for path in sources
            if is_test_file(path, testpaths) and module_name(path) in reached
        ),
    )
    chosen: list[str] = []
    left_out: list[str] = []
    spent = 0.0
    for _, seconds, path in candidates:
        if path in touched or budget.wall(spent + seconds) <= budget.seconds:
            chosen.append(path)
            spent += seconds
        else:
            left_out.append(path)
    return Selection(tuple(sorted(chosen)), tuple(sorted(left_out)), budget.wall(spent))


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        message = "git is not on PATH"
        raise SystemExit(message)
    # lint-waiver: LW-164101 [S603]; Git runs with fixed argv and no shell; the base ref is a caller-supplied branch name. Reimplementing diff and ls-files would duplicate Git.
    return subprocess.run(  # noqa: S603
        [git, "-C", str(REPO_ROOT), *args], check=True, capture_output=True, text=True
    ).stdout


def changed_paths(base_ref: str) -> list[str]:
    """Paths changed since the merge base with `base_ref`, plus staged, unstaged and new files."""
    committed = _git("diff", "--name-only", "-z", f"{base_ref}...HEAD").split("\0")
    local = _git("diff", "--name-only", "-z", "HEAD").split("\0")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({path for path in [*committed, *local, *untracked] if path})


def read_sources() -> dict[str, str]:
    """Every tracked Python file that exists in the working tree, by repository path."""
    sources: dict[str, str] = {}
    for path in _git("ls-files", "-z", "*.py").split("\0"):
        file = REPO_ROOT / path
        if path and file.is_file():
            sources[path] = file.read_text(encoding="utf-8", errors="replace")
    return sources


def testpaths() -> list[str]:
    """The directories the main suite collects, from `[tool.pytest.ini_options] testpaths`."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(config["tool"]["pytest"]["ini_options"]["testpaths"])


def _selection(args: argparse.Namespace) -> Selection:
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    return select(
        changed_paths(args.base_ref),
        read_sources(),
        durations,
        testpaths=testpaths(),
        budget=Budget(args.budget_seconds, args.workers),
    )


def _report(selection: Selection) -> None:
    print(
        f"impacted tests: {len(selection.files)} files, about {selection.seconds:.0f} s; "
        f"{len(selection.left_out)} more files left to the merge queue",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Print or run the impacted tests; the exit status is pytest's."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("select", "run"))
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--budget-seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    selection = _selection(args)
    _report(selection)
    if args.mode == "select" or not selection.files:
        print("\n".join(selection.files))
        return 0
    # lint-waiver: LW-164102 [S603]; Runs this interpreter's pytest on files this script selected from tracked paths; no shell.
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            str(args.workers),
            "--no-cov",
            *selection.files,
        ],
        cwd=REPO_ROOT,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
