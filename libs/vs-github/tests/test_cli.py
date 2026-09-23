"""Tests for the GitHub CLI abstraction."""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003  # tracked: #288

import pytest

from vs_github.api import (
    GitHubAuthenticationError,
    GitHubCLI,
    GitHubCLIError,
    GitHubCLIUnavailableError,
)


class RecordingRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # noqa: ANN003  # tracked: #288
        self.calls.append((command, kwargs.get("cwd")))
        return next(self.results)


def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):  # noqa: ANN202  # tracked: #288
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def test_create_repository_checks_authentication_then_creates(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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


def test_current_user_checks_authentication_then_reads_login():  # noqa: ANN201  # tracked: #288
    runner = RecordingRunner([_result(), _result(stdout="octocat\n")])

    assert GitHubCLI(_runner=runner).current_user() == "octocat"
    assert runner.calls == [
        (["gh", "auth", "status", "--hostname", "github.com"], None),
        (["gh", "api", "user", "--jq", ".login"], None),
    ]


def test_current_user_rejects_empty_login():  # noqa: ANN201  # tracked: #288
    runner = RecordingRunner([_result(), _result(stdout="\n")])

    with pytest.raises(GitHubCLIError, match="empty authenticated user"):
        GitHubCLI(_runner=runner).current_user()


def test_clone_repository_reports_unauthenticated_user(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    runner = RecordingRunner([_result(1, stderr="not logged into any GitHub hosts")])

    with pytest.raises(GitHubAuthenticationError, match=r"gh auth login.*not logged"):
        GitHubCLI(_runner=runner).clone_repository("owner/trial", tmp_path / "trial")

    assert len(runner.calls) == 1


def test_repository_error_includes_gh_detail(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    runner = RecordingRunner([_result(), _result(1, stderr="name already exists")])

    with pytest.raises(GitHubCLIError, match="name already exists"):
        GitHubCLI(_runner=runner).clone_repository("owner/trial", tmp_path / "trial")


def test_missing_gh_has_install_guidance():  # noqa: ANN201  # tracked: #288
    def missing_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202  # tracked: #288
        raise FileNotFoundError("gh")

    with pytest.raises(GitHubCLIUnavailableError, match="https://cli.github.com"):  # noqa: RUF043  # tracked: #288
        GitHubCLI(_runner=missing_runner).ensure_authenticated()
