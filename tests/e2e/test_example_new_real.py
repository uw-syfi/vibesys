"""``vibesys example new`` with real Git: the copy contract, and a catalog example end to end."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from tests.support import run_test_command
from tests.support.example_materializer import (
    check_copy_tracked_contract,
    check_repository_root_contract,
)

from entrypoints.examples import GitExampleMaterializer, default_checkout, run
from vs_project.api import Git
from vs_sim.api import OsProcesses
from vs_sim.api.testing import HANG_GUARD_S


def _git(cwd: Path, *args: str) -> str:
    return run_test_command(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD_S,
    ).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the initial commit an identity on machines without a Git configuration."""
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example-test@localhost")


def test_git_materializer_passes_the_copy_contract(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    example = checkout / "examples" / "demo"
    (example / "src").mkdir(parents=True)
    (example / "src" / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
    (example / "README.md").write_text("demo\n", encoding="utf-8")
    _git(checkout, "init", "--quiet")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "--message", "catalog")

    check_copy_tracked_contract(
        GitExampleMaterializer(Git(OsProcesses())),
        checkout,
        PurePosixPath("examples/demo"),
        tmp_path / "out",
    )


def test_git_materializer_passes_the_repository_root_contract(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / "nested").mkdir(parents=True)
    _git(repository, "init", "--quiet")
    outside = tmp_path / "outside"
    outside.mkdir()

    check_repository_root_contract(GitExampleMaterializer(Git(OsProcesses())), repository, outside)


def test_untracked_files_stay_behind_and_the_copy_is_one_clean_commit(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    example = checkout / "examples" / "demo"
    example.mkdir(parents=True)
    (example / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(checkout, "init", "--quiet")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "--message", "catalog")
    (example / "build-output.so").write_text("untracked\n", encoding="utf-8")
    destination = tmp_path / "project"

    GitExampleMaterializer(Git(OsProcesses())).copy_tracked(
        checkout, PurePosixPath("examples/demo"), destination, message="Start"
    )

    assert sorted(path.name for path in destination.iterdir()) == [".git", "kept.txt"]
    assert _git(destination, "log", "--format=%s").splitlines() == ["Start"]
    assert _git(destination, "status", "--porcelain") == ""


def test_new_from_the_real_catalog_creates_a_runnable_project(tmp_path: Path) -> None:
    destination = tmp_path / "stack"

    code, stdout, stderr = run(
        ["new", "data-structures/stack", str(destination)],
        cwd=tmp_path,
        checkout=default_checkout(),
    )

    assert (code, stderr) == (0, "")
    assert f"  cd {destination}" in stdout.splitlines()
    assert "  vibesys --task mpmc" in stdout.splitlines()
    assert sorted(path.name for path in (destination / ".vibesys" / "tasks").iterdir()) == [
        "mpmc",
        "mpsc",
        "spmc",
        "spsc",
    ]
    assert (destination / "src" / "lib.rs").is_file()
    assert _git(destination, "log", "--format=%s").splitlines() == [
        "Start from VibeSys example data-structures/stack"
    ]
