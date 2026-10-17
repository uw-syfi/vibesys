"""Git that a test starts ignores the developer's Git config, and no test can turn that off.

A developer's global config can carry a failing hook, forced commit signing, or another
default branch. Without isolation, 25 of 536 git-using tests failed under such a config.
`tests/support/isolated_environment.py` points every test process and its children at an
empty global config; these tests prove that, and that no test file resets the variables.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
from tests.support import run_test_command
from tests.support.child_pytest import BARE_PYTEST, child_pythonpath
from tests.support.isolated_environment import HERMETIC_GIT_VARIABLES

pytest_plugins = ["pytester"]

_REPOSITORY = Path(__file__).resolve().parents[2]

_INNER_TEST = """
import subprocess

def test_a_commit_succeeds(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True, capture_output=True,
    )
"""


def _hostile_global_config(directory: Path) -> Path:
    """A config with a failing pre-commit hook, forced signing with a failing signer."""
    hooks = directory / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    config = directory / "hostile-gitconfig"
    config.write_text(
        f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n"
        "[gpg]\n\tprogram = /bin/false\n[user]\n\tname = x\n\temail = x@example.invalid\n",
        encoding="utf-8",
    )
    return config


@pytest.mark.parametrize("isolated", [False, True], ids=["control-without", "with-isolation"])
def test_a_commit_succeeds_under_a_hostile_global_config(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, isolated: bool
) -> None:
    """The control shows the config bites; with the suite plugin the same commit succeeds."""
    hostile = _hostile_global_config(pytester.path)
    for variable in HERMETIC_GIT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.setenv("PYTHONPATH", child_pythonpath(_REPOSITORY))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    pytester.makepyfile(test_inner=_INNER_TEST)
    plugins = ["-p", "tests.support.isolated_environment"] if isolated else []

    result = pytester.runpytest_subprocess(*BARE_PYTEST, *plugins, "-q")

    result.assert_outcomes(**({"passed": 1} if isolated else {"failed": 1}))


def test_the_session_points_git_at_an_empty_global_config() -> None:
    config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    assert config.read_text(encoding="utf-8") == ""
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    listed = run_test_command(
        ["git", "config", "--show-origin", "--list"],
        capture_output=True,
        text=True,
        check=True,
        cwd=config.parent,
    ).stdout
    assert [line for line in listed.splitlines() if not line.startswith("command line:")] == []


_RESETTABLE = re.compile(r"GIT_CONFIG_(GLOBAL|NOSYSTEM)")
_OWNERS = {
    "tests/support/isolated_environment.py",
    "tests/quality/test_git_hermetic.py",
}


def test_no_test_resets_the_git_isolation_variables() -> None:
    """Only the suite plugin names `GIT_CONFIG_GLOBAL` or `GIT_CONFIG_NOSYSTEM`.

    A test that must exercise Git config sets per-command `-c` options or a repository
    config instead, so the session isolation stays in force for every other test.
    """
    roots = [_REPOSITORY / "tests", *sorted((_REPOSITORY / "libs").glob("*/tests"))]
    roots += sorted((_REPOSITORY / "sdk").glob("*/tests"))
    offenders = [
        str(path.relative_to(_REPOSITORY))
        for root in roots
        for path in root.rglob("*.py")
        if str(path.relative_to(_REPOSITORY)) not in _OWNERS
        and _RESETTABLE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
