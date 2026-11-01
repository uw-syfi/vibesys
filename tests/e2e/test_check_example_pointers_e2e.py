"""The example-pointer check, run over a real git repository as #3568 left one.

A change that commits a worktree's checked-out submodule commit moves a pin nobody meant to move.
The check must see that through the Git policy the product sets (`diff.ignoreSubmodules=all`),
which a fake git cannot show.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from scripts.check_example_pointers import main, pointer_changes, run_git, undeclared
from tests.support.real_git import REAL_GIT

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_PIN = "examples/family/repositories/masa"
_GITLINK = "160000"


def _run(root: Path, *args: str) -> str:
    return REAL_GIT.run_text(list(args), cwd=root).check().stdout.strip()


def _commit_pin(root: Path, commit: str, message: str) -> None:
    _run(root, "update-index", "--add", "--cacheinfo", f"{_GITLINK},{commit},{_PIN}")
    # The index holds the pin although Git, with diff.ignoreSubmodules=all, sees nothing changed.
    _run(root, "commit", "-q", "--allow-empty", "-m", message)


def test_a_change_that_commits_the_checked_out_pin_fails_unless_it_declares_the_bump(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "init", "-q", "--initial-branch=main")
    _run(tmp_path, "config", "user.name", "Pointer fixture")
    _run(tmp_path, "config", "user.email", "pointer@example.invalid")
    (tmp_path / "README.md").write_text("base\n")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-q", "-m", "base")
    _commit_pin(tmp_path, "1" * 40, "main: pin the example")
    _run(tmp_path, "checkout", "-q", "-b", "change")
    git = partial(run_git, tmp_path)

    (tmp_path / "README.md").write_text("change\n")
    _run(tmp_path, "commit", "-qam", "change: an ordinary edit")
    assert pointer_changes(git, "main")[0] == []

    _commit_pin(tmp_path, "2" * 40, "change: record the worktree's commit")
    changed, message = pointer_changes(git, "main")
    assert changed == [_PIN]
    assert undeclared(changed, message) == changed
    assert main(["--base-ref", "main"], git=git) == 1
    assert _PIN in capsys.readouterr().out

    _commit_pin(tmp_path, "3" * 40, f"change: bump masa\n\nExample-Bump: {_PIN}")
    assert main(["--base-ref", "main"], git=git) == 0
