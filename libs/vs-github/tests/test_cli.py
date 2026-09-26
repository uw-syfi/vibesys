"""Tests for the GitHub CLI abstraction."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Never

import pytest

from vs_github.api import (
    GitHubAuthenticationError,
    GitHubCLI,
    GitHubCLIError,
    GitHubCLIUnavailableError,
)

if TYPE_CHECKING:
    from pathlib import Path


class RecordingRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text
        self.calls.append((command, cwd))
        return next(self.results)


def _result(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def test_create_repository_checks_authentication_then_creates(tmp_path: Path) -> None:
    runner = RecordingRunner([_result(), _result()])
    github = GitHubCLI(_runner=runner)

    github.create_repository(
        "vibesys-playground/trial",
        visibility="internal",
        source=tmp_path,
    )

    assert runner.calls == [
        (["gh", "auth", "status", "--hostname", "github.com"], None),
        (
            [
                "gh",
                "repo",
                "create",
                "vibesys-playground/trial",
                "--internal",
                "--source",
                str(tmp_path),
                "--remote",
                "origin",
            ],
            tmp_path,
        ),
    ]


def test_current_user_checks_authentication_then_reads_login() -> None:
    runner = RecordingRunner([_result(), _result(stdout="octocat\n")])

    assert GitHubCLI(_runner=runner).current_user() == "octocat"
    assert runner.calls == [
        (["gh", "auth", "status", "--hostname", "github.com"], None),
        (["gh", "api", "user", "--jq", ".login"], None),
    ]


def test_current_user_rejects_empty_login() -> None:
    runner = RecordingRunner([_result(), _result(stdout="\n")])

    with pytest.raises(GitHubCLIError, match="empty authenticated user"):
        GitHubCLI(_runner=runner).current_user()


def test_clone_repository_reports_unauthenticated_user(tmp_path: Path) -> None:
    runner = RecordingRunner([_result(1, stderr="not logged into any GitHub hosts")])

    with pytest.raises(GitHubAuthenticationError, match=r"gh auth login.*not logged"):
        GitHubCLI(_runner=runner).clone_repository("owner/trial", tmp_path / "trial")

    assert len(runner.calls) == 1


def test_repository_error_includes_gh_detail(tmp_path: Path) -> None:
    runner = RecordingRunner([_result(), _result(1, stderr="name already exists")])

    with pytest.raises(GitHubCLIError, match="name already exists"):
        GitHubCLI(_runner=runner).clone_repository("owner/trial", tmp_path / "trial")


def test_missing_gh_has_install_guidance() -> None:
    def missing_runner(*_args: object, **_kwargs: object) -> Never:
        raise FileNotFoundError("gh")

    with pytest.raises(GitHubCLIUnavailableError, match=r"https://cli\.github\.com"):
        GitHubCLI(_runner=missing_runner).ensure_authenticated()


def test_issue_operations_use_explicit_repository_and_preserve_json() -> None:
    runner = RecordingRunner(
        [
            _result(stdout='[{"number": 8}]'),
            _result(stdout='{"number": 8}'),
            _result(stdout="https://github.com/owner/repo/issues/9\n"),
            _result(),
            _result(),
            _result(),
            _result(),
        ]
    )
    github = GitHubCLI(_runner=runner)

    assert github.list_issues("owner/repo") == [{"number": 8}]
    assert github.view_issue("owner/repo", 8) == {"number": 8}
    assert (
        github.create_issue("owner/repo", title="title", body="body", labels=["vibesys:type/bug"])
        == 9
    )
    github.edit_issue("owner/repo", 8, add_labels=["vibesys:status/blocked"])
    github.comment_issue("owner/repo", 8, "metadata")
    github.set_issue_state("owner/repo", 8, issue_open=False)
    github.ensure_label("owner/repo", "vibesys:type/bug")

    assert [call[0][1:4] for call in runner.calls] == [
        ["issue", "list", "--repo"],
        ["issue", "view", "8"],
        ["issue", "create", "--repo"],
        ["issue", "edit", "8"],
        ["issue", "comment", "8"],
        ["issue", "close", "8"],
        ["label", "create", "vibesys:type/bug"],
    ]
