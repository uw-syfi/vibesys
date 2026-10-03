"""Architecture contract: VibeSys starts Git only through ``vs_project``'s ``run_git``.

Git runs auto maintenance after commits and, from Git 2.47, detaches it. The
detached process races whoever removes or moves the repository next. The
``vs_project._git_process`` module is the single place that turns it off, so a
Git subprocess started anywhere else under ``src/`` or ``libs/`` is a defect.

The scan is syntactic and flags, outside the helper module:

* a ``subprocess`` call whose argv starts with ``"git"``, a name that holds the
  Git executable (``git``), or ``shutil.which("git")``;
* any other argv list that starts with those, unless it is the direct argument
  of a ``.run(...)`` call (``GitTracker.run`` reaches the helper) or its module
  imports ``run_git`` or ``git_environment``, so the argv is handed to code that
  builds its environment with the helper.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_HELPER = Path("libs/vs-project/src/vs_project/_git_process.py")
_SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_HELPER_NAMES = frozenset({"run_git", "git_environment"})
_GIT_NAMES = frozenset({"git", "git_bin", "git_exe", "git_executable"})


def _starts_with_git(node: ast.expr) -> bool:
    """Return whether an argv expression begins with the Git executable."""
    if isinstance(node, ast.List | ast.Tuple) and node.elts:
        return _is_git_executable(node.elts[0])
    return False


def _is_git_executable(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == "git"
    if isinstance(node, ast.Name):
        return node.id in _GIT_NAMES
    if isinstance(node, ast.BoolOp):
        return any(_is_git_executable(value) for value in node.values)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "which"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "git"
    )


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _SUBPROCESS_CALLS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def git_invocation_sites(source: str) -> list[int]:
    """Return line numbers where ``source`` starts Git outside the helper."""
    tree = ast.parse(source)
    tracker_argv: set[int] = set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "run" and node.args:
            tracker_argv.add(id(node.args[0]))
        if _is_subprocess_call(node) and node.args and _starts_with_git(node.args[0]):
            lines.add(node.lineno)
    uses_helper = any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("vs_project")
        and any(alias.name in _HELPER_NAMES for alias in node.names)
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if (
            not uses_helper
            and isinstance(node, ast.List | ast.Tuple)
            and id(node) not in tracker_argv
            and _starts_with_git(node)
            and not (isinstance(node.elts[0], ast.Name) and len(node.elts) == 1)
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _scanned_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for root in (repo_root / "src", *(repo_root / "libs").glob("*/src")):
        files.extend(path for path in root.rglob("*.py") if path != repo_root / _HELPER)
    return sorted(files)


def test_every_git_subprocess_goes_through_the_project_helper(repo_root: Path) -> None:
    violations = [
        f"{path.relative_to(repo_root).as_posix()}:{line}"
        for path in _scanned_files(repo_root)
        for line in git_invocation_sites(path.read_text(encoding="utf-8"))
    ]
    assert violations == [], (
        "Start Git through vs_project.api.run_git so background maintenance stays off:\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "source",
    [
        'import subprocess\nsubprocess.run(["git", "status"])\n',
        'import subprocess\nsubprocess.check_output(("git", "log"))\n',
        'import shutil, subprocess\ngit = shutil.which("git") or "git"\nsubprocess.run([git, *a])\n',
        'import subprocess\nsubprocess.Popen([shutil.which("git"), "x"])\n',
        'runner(["git", "push"])\n',
        'from vs_project.api import run_git\nimport subprocess\nsubprocess.run(["git", "x"])\n',
    ],
)
def test_scan_flags_direct_git_argv(source: str) -> None:
    assert git_invocation_sites(source) != []


@pytest.mark.parametrize(
    "source",
    [
        'tracker.run(["git", "status"])\n',
        'from vs_project.api import run_git\ncmd = ["git", "add"]\ntracker.run(cmd)\n',
        'import subprocess\nsubprocess.run(["ls", "-l"])\n',
        "import subprocess\nsubprocess.run(cmd)\n",
    ],
)
def test_scan_accepts_tracker_and_other_commands(source: str) -> None:
    assert git_invocation_sites(source) == []
