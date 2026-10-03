"""The ``loose_git_objects`` fixture pins a Git policy that keeps objects loose."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _git_config(repo: Path, key: str) -> str:
    git = shutil.which("git")
    assert git is not None
    return subprocess.run(  # noqa: S603  # lint-waiver: LW-961033 [S603]; fixed Git argv reads one config value without a shell.
        [git, "config", "--get", key],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.usefixtures("loose_git_objects")
def test_automatic_maintenance_is_off_for_a_new_repository(tmp_path: Path) -> None:
    """Auto maintenance packs loose objects after a commit, which would hide the
    one object a test deletes; the fixture's policy applies to every repository.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], cwd=repo, check=True)  # noqa: S603  # lint-waiver: LW-961034 [S603]; fixed Git argv creates a scratch repository without a shell.

    assert _git_config(repo, "maintenance.auto") == "false"
    assert _git_config(repo, "gc.auto") == "0"
