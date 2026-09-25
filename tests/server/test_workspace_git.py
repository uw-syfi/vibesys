"""Read-only patch reader tests against real git repositories."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from server.api.workspace_git import WorkspacePatchReader

if TYPE_CHECKING:
    from pathlib import Path


class _Warnings:
    """Capture reader warnings in the tracker events' format."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, summary: str, *, detail: str | None = None) -> None:
        self.messages.append(summary if detail is None else f"{summary}: {detail}")


def _git(workspace: Path, *args: str) -> str:
    command = ["git", "-C", str(workspace), *args]
    result = run_test_command(command, capture_output=True, check=True, text=True)
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(path: Path, message: str) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def test_diff_patch_reads_one_file_between_commits(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    (workspace / "lib.rs").write_text("old\n", encoding="utf-8")
    (workspace / "other.rs").write_text("untouched\n", encoding="utf-8")
    base = _commit_all(workspace, "baseline")
    (workspace / "lib.rs").write_text("new\n", encoding="utf-8")
    (workspace / "other.rs").write_text("also changed\n", encoding="utf-8")
    head = _commit_all(workspace, "round 1")
    warnings = _Warnings()

    output = WorkspacePatchReader(workspace, warning=warnings).diff_patch(base, head, ("lib.rs",))

    assert output is not None
    assert "+new" in output
    # The pathspec limits the patch to the requested file.
    assert "other.rs" not in output
    assert warnings.messages == []


def test_diff_patch_rejects_a_revision_expression(tmp_path: Path) -> None:
    reader = WorkspacePatchReader(_repo(tmp_path / "workspace"), warning=_Warnings())

    with pytest.raises(ValueError, match="not a commit object name"):
        reader.diff_patch("HEAD~1", "a" * 40, ("lib.rs",))
    with pytest.raises(ValueError, match="not a commit object name"):
        reader.diff_patch("a" * 40, "--output=escape", ("lib.rs",))


def test_diff_patch_treats_paths_as_literals(tmp_path: Path) -> None:
    """A glob-looking path selects only itself, never other files."""
    workspace = _repo(tmp_path / "workspace")
    (workspace / "*.rs").write_text("literal star\n", encoding="utf-8")
    (workspace / "lib.rs").write_text("old\n", encoding="utf-8")
    base = _commit_all(workspace, "baseline")
    (workspace / "*.rs").write_text("star changed\n", encoding="utf-8")
    (workspace / "lib.rs").write_text("new\n", encoding="utf-8")
    head = _commit_all(workspace, "round 1")

    output = WorkspacePatchReader(workspace, warning=_Warnings()).diff_patch(base, head, ("*.rs",))

    assert output is not None
    assert "star changed" in output
    assert "lib.rs" not in output


def test_diff_patch_reports_an_unresolvable_range(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "workspace")
    warnings = _Warnings()

    output = WorkspacePatchReader(workspace, warning=warnings).diff_patch(
        "a" * 40, "b" * 40, ("lib.rs",)
    )

    assert output is None
    assert [line for line in warnings.messages if "read-only patch exit" in line]


def test_diff_patch_reports_a_missing_repository(tmp_path: Path) -> None:
    """A recorded run whose workspace is gone degrades, it does not raise."""
    warnings = _Warnings()

    output = WorkspacePatchReader(tmp_path / "absent", warning=warnings).diff_patch(
        "a" * 40, "b" * 40, ("lib.rs",)
    )

    assert output is None
    assert [line for line in warnings.messages if "read-only patch failed" in line]


@pytest.mark.parametrize(
    "failure", [OSError("git is missing"), subprocess.TimeoutExpired(["git"], 10.0)]
)
def test_diff_patch_reports_a_failed_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    workspace = _repo(tmp_path / "workspace")
    warnings = _Warnings()
    reader = WorkspacePatchReader(workspace, warning=warnings)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(subprocess, "run", explode)

    assert reader.diff_patch("a" * 40, "b" * 40, ("lib.rs",)) is None
    assert [line for line in warnings.messages if "read-only patch failed" in line]
